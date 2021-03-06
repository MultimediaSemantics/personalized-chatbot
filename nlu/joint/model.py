import sys
import traceback
import tensorflow as tf
from tensorflow.contrib import layers
import numpy as np
from tensorflow.contrib.rnn import BasicLSTMCell, LSTMStateTuple, GRUCell
from .embeddings import EmbeddingsFromScratch, FixedEmbeddings, FineTuneEmbeddings, spacy_wrapper

flatten = lambda l: [item for sublist in l for item in sublist]

class Model:
    def __init__(self, input_steps, embedding_size, hidden_size, vocabs, word_embeddings, recurrent_cell, attention, multi_turn=False, batch_size=None, intent_combination=None):
        # save the parameters
        self.input_steps = input_steps
        self.embedding_size = embedding_size
        self.hidden_size = hidden_size
        self.batch_size = batch_size
        # also save the vocabularies, used by embedders
        self.vocabs = vocabs
        self.input_embedding_size = 300
        # one between 'large', 'small', 'medium', 'cnr' (all fixed pretrained) or 'random' (trainable)
        self.word_embeddings = word_embeddings
        # one between lstm and gru
        self.recurrent_cell = recurrent_cell
        # one between intents, slots, both, none
        if attention == 'intents' or attention == 'both':
            self.intent_attention = True
        else:
            self.intent_attention = False
        if attention == 'slots' or attention == 'both':
            self.slots_attention = True
        else:
            self.slots_attention = False

        # this variable changes the architecture from single turn to multi-turn
        self.multi_turn = multi_turn
        # choose between RNN or CRF for the intent combination
        self.intent_combination = (intent_combination or 'gru') if multi_turn else None

        # define the placeholders for inputs to the graph
        # the input words are a tensor of type string.
        # In this way the one_hot encoding stuff and embeddings are managed by the embedding classes.
        # This makes the input always to be strings, both when the embeddings are part of the model
        # both when are precomputed
        self.words_inputs = tf.placeholder(tf.string, [input_steps, batch_size], name="words_inputs")
        # This placeholder is for the actual length of each sentence, used in decoding
        self.encoder_inputs_actual_length = tf.placeholder(tf.int32, [batch_size], name='encoder_inputs_actual_length')
        # Placeholder for the output sequence, used in train mode as truth value
        self.decoder_targets = tf.placeholder(tf.string, [batch_size, input_steps], name='decoder_targets')
        # Placeholder for the output intent, used in train mode as truth value
        self.intent_targets = tf.placeholder(tf.string, [batch_size], name='intent_targets')

        if self.multi_turn:
            # this parameter will help understanding what is bot turn and what is user turn, never used
            self.bot_turn_actual_length = tf.placeholder(tf.int32, [batch_size], name='bot_turn_actual_length')
            # this instead is the previous intent
            self.previous_intent = tf.placeholder(tf.string, [batch_size], name='previous_intent')
        

    def build(self, tokenizer='space', language='en'):
        # get the tensor for batch size
        batch_size_tensor = tf.shape(self.words_inputs)[1]

        # unpack the vocabularies
        input_vocab, slot_vocab, intent_vocab = self.vocabs

        # then create the embeddings and mapper (one-hot index to words and viceversa) for each one of them
        # For input words embedder, can choose between EmbeddingsFromScratch, FixedEmbeddings, FineTueEmbeddings:
        # choose if input words are trained as part of the model from scratch, or come precomputed, or precomputed+linear transformation
        #self.wordsEmbedder = FineTuneEmbeddings(tokenizer, language)
        if self.word_embeddings == 'random':
            self.wordsEmbedder = EmbeddingsFromScratch(input_vocab, 'words', self.input_embedding_size, True)
        else:
            self.wordsEmbedder = FixedEmbeddings(tokenizer, language, self.word_embeddings)
        self.input_embedding_size = self.wordsEmbedder.embedding_size
        self.slotEmbedder = EmbeddingsFromScratch(slot_vocab, 'slot', self.embedding_size, True)
        print('intent vocab', intent_vocab)
        self.intentEmbedder = EmbeddingsFromScratch(intent_vocab, 'intent', self.embedding_size)

        # the embedded inputs
        self.encoder_inputs_embedded = self.wordsEmbedder.get_word_embeddings(self.words_inputs)


        # The intent gold values
        intent_ids_targets = self.intentEmbedder.get_indexes_from_words_tensor(self.intent_targets)

        # Encoder

        # Definition of cells used for bidirectional RNN encoder
        if self.recurrent_cell == 'lstm':
            encoder_f_cell = BasicLSTMCell(self.hidden_size)
            encoder_b_cell = BasicLSTMCell(self.hidden_size)
            self.hidden_state_size = self.hidden_size * 2
        elif self.recurrent_cell == 'gru':
            encoder_f_cell = GRUCell(self.hidden_size)
            encoder_b_cell = GRUCell(self.hidden_size)
            self.hidden_state_size = self.hidden_size
        else:
            raise ValueError('invalid cell of type ' + self.recurrent_cell)

        # Bidirectional RNN
        # The size of the following four variables：T*B*D，T*B*D，B*D，B*D
        (encoder_fw_outputs, encoder_bw_outputs), (encoder_fw_final_state, encoder_bw_final_state) = \
            tf.nn.bidirectional_dynamic_rnn(cell_fw=encoder_f_cell,
                                            cell_bw=encoder_b_cell,
                                            inputs=self.encoder_inputs_embedded,
                                            sequence_length=self.encoder_inputs_actual_length,
                                            dtype=tf.float32, time_major=True)

        # Encoder outputs

        # The encoder outputs are the concatenation of the outputs of each direction.
        # The concatenation is done on the third dimension. Dimensions: (time, batch, hidden_size)
        encoder_outputs = tf.concat((encoder_fw_outputs, encoder_bw_outputs), 2)
        # Also concatenate things for the final state. Dimensions: (batch, hidden_size)
        if self.recurrent_cell == 'lstm':
            encoder_final_state_c = tf.concat(
                (encoder_fw_final_state.c, encoder_bw_final_state.c), 1)
            encoder_final_state_h = tf.concat(
                (encoder_fw_final_state.h, encoder_bw_final_state.h), 1)
            self.encoder_final_state = LSTMStateTuple(c=encoder_final_state_c, h=encoder_final_state_h)
        elif self.recurrent_cell == 'gru':
            encoder_final_state_h = tf.concat((encoder_fw_final_state, encoder_bw_final_state), 1)
            self.encoder_final_state = encoder_final_state_h


        # Intent output
        
        # Define the weights and biases to perform the output projection on the intent output
        intent_W = tf.get_variable('intent_W', initializer=tf.random_uniform([self.hidden_size * 2, self.intentEmbedder.vocab_size], -0.1, 0.1),
                               dtype=tf.float32)
        intent_b = tf.get_variable("intent_b", initializer=tf.zeros([self.intentEmbedder.vocab_size]), dtype=tf.float32)

        # perform the feed-forward layer
        intent_logits = tf.add(tf.matmul(encoder_final_state_h, intent_W), intent_b)
        if self.intent_combination == 'crf':
            previous_intent_ids = self.intentEmbedder.get_indexes_from_words_tensor(self.previous_intent)
            #print('shape of previous_intent_ids', tf.shape(previous_intent_ids))
            previous_intent_one_hot = tf.one_hot(previous_intent_ids, depth=self.intentEmbedder.vocab_size, dtype=tf.float32, axis=1)
            # transpose from (intent_n, batch_size) to (batch_size, intent_n)
            #previous_intent_one_hot = tf.transpose(previous_intent_one_hot, [1, 0])
            #print('shape of previous_intent_one_hot', tf.shape(previous_intent_one_hot), 'intent_dict_size', self.intentEmbedder.vocab_size)
            # the unary scores are [previous_intent_logits, current_intent_logits] put together in shape (batch_size,2,intent_n)
            unary_scores = tf.stack([previous_intent_one_hot, intent_logits], 1)
            #print('shape of unary scores', tf.shape(unary_scores))
            #unary_scores = tf.transpose(unary_scores, [])
            gold_tags = tf.stack([previous_intent_ids, intent_ids_targets], 1)
            #print('shape of gold tags', tf.shape(gold_tags))
            # cast the gold tags to in32
            gold_tags = tf.to_int32(gold_tags)
            #sequence_lengths = tf.constant(2, dtype=tf.int32, shape=[self.batch_size])
            sequence_lengths = tf.fill((batch_size_tensor,), 2)
            log_likelihood, transition_params = tf.contrib.crf.crf_log_likelihood(unary_scores, gold_tags, sequence_lengths)
            #print(log_likelihood, transition_params)
            # the loss of the CRF is kept to the backpropagation
            loss_crf = tf.reduce_mean(-log_likelihood)
            #unary_real_scores = tf.contrib.crf.crf_unary_score(gold_tags, sequence_lengths, unary_scores)
            #print('unary_real_scores', tf.shape(unary_real_scores))
            viterbi_sequence, viterbi_score = tf.contrib.crf.crf_decode(unary_scores, transition_params, sequence_lengths)
            # transpose from (batch, time) to (time, batch)
            intents_major_timesteps = tf.transpose(viterbi_sequence, [1, 0])
            #print('intents_major_timesteps', tf.shape(intents_major_timesteps))
            # take the output intent, intents_major_timesteps should be [previous_intent, current_intent]
            intent_id = intents_major_timesteps[1]
            intent_id = tf.to_int64(intent_id)
        else:
            if self.multi_turn:
                # in this case some more steps need to be done before argmax/softmax
                previous_intent_ids = self.intentEmbedder.get_indexes_from_words_tensor(self.previous_intent)
                previous_intent_one_hot = tf.one_hot(previous_intent_ids, depth=self.intentEmbedder.vocab_size, dtype=tf.float32)
                if self.intent_combination == 'gru':
                    self.intent_combiner = GRUCell(self.intentEmbedder.vocab_size)
                    # apply the GRU cell once: from input (current logits, previous intent as state) to output (next logits, next output the same as next logits in GRU)
                    #print(intent_logits)
                    #self.intent_combiner.build()
                    intent_logits, _ = self.intent_combiner(intent_logits, previous_intent_one_hot)
                else:
                    # LSTM
                    self.intent_combiner = BasicLSTMCell(self.intentEmbedder.vocab_size)
                    previous_state = LSTMStateTuple(c=previous_intent_one_hot, h=previous_intent_one_hot)
                    intent_logits, _ = self.intent_combiner(intent_logits, previous_state)
                #print(intent_logits)
            # take the argmax
            intent_id = tf.argmax(intent_logits, axis=1)
        # and translate to the corresponding string
        self.intent = self.intentEmbedder.get_words_from_indexes(intent_id)
        # make this tensor retrievable by name at test time
        self.intent = tf.identity(self.intent, name="intent")
        # also evaluate the classification score
        intent_scores = tf.reduce_max(tf.nn.softmax(intent_logits), axis=1, name="intent_score")


        # Slot label decoder

        decoder_lengths = self.encoder_inputs_actual_length

        # Initial values to provide to the decoding stage
        # generate a tensor of batch_size * 'O' for start of sentence.
        # This value will be passed to first iteration of decoding in place of the previous slot label
        sos_time_slice = tf.fill((batch_size_tensor,), 'O')


        # the following functions are used by the CustomHelper
        def initial_fn():
            """
            defines how to provide the input to the decoder RNN cell at time 0
            """
            initial_elements_finished = (0 >= decoder_lengths)  # all False at the initial step
            # get the embedded representation of the initial fake previous-output-label
            sos_step_embedded = self.slotEmbedder.get_word_embeddings(sos_time_slice)
            # then concatenate it with the encoder output at time 0
            initial_input = tf.concat((sos_step_embedded, encoder_outputs[0]), 1)
            return initial_elements_finished, initial_input

        def sample_fn(time, outputs, state):
            """
            defines how to sample from the output of the RNN cell
            """
            # take the argmax from the logits
            prediction_id = tf.to_int32(tf.argmax(outputs, axis=1))
            return prediction_id

        def next_inputs_fn(time, outputs, state, sample_ids):
            """
            defines how to provide the input to the RNN cell at timesteps>0
            """
            # From the last output, represented by sample_ids, get its embedded value
            pred_embedding = self.slotEmbedder.get_word_embeddings_from_ids(sample_ids)
            # Now concatenate it with the output of the decoder at the current timestep.
            # This is the new input to the RNN cell
            next_inputs = tf.concat((pred_embedding, encoder_outputs[time]), 1)
            # Establish which samples in the batch have already finished the decoding
            elements_finished = (time >= decoder_lengths)  # this operation produces boolean tensor of [batch_size]
            # don't modify the state
            next_state = state
            return elements_finished, next_inputs, next_state

        # Build the helper with the declared functions
        my_helper = tf.contrib.seq2seq.CustomHelper(initial_fn, sample_fn, next_inputs_fn)

        # Decoding function
        def decode(helper):
            # The decoding LSTM cell
            if self.recurrent_cell == 'lstm':
                cell = BasicLSTMCell(num_units=self.hidden_state_size)
            elif self.recurrent_cell == 'gru':
                cell = GRUCell(num_units=self.hidden_state_size)
            if self.slots_attention:
                # Get the memory representation (for the attention) by making the
                # encoder outputs dimensions from (time, batch, hidden_size) to (batch, time, hidden_size)
                memory = tf.transpose(encoder_outputs, [1, 0, 2])
                # Use the BahdanauAttention on the memory
                attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
                    num_units=self.hidden_size, memory=memory,
                    memory_sequence_length=self.encoder_inputs_actual_length)
                # that gets wrapped inside the attention mechanism
                attn_cell = tf.contrib.seq2seq.AttentionWrapper(
                    cell, attention_mechanism, attention_layer_size=self.hidden_size)
                # and gets wrapped inside a output projection wrapper (weights+biases),
                # to have an output with logits on the slot labels dimension
            else:
                # no attention
                attn_cell = cell
            out_cell = tf.contrib.rnn.OutputProjectionWrapper(
                attn_cell, self.slotEmbedder.vocab_size
            )
            # Define the decoder by combining the helper with the RNN cell
            decoder = tf.contrib.seq2seq.BasicDecoder(
                cell=out_cell, helper=helper,
                initial_state=out_cell.zero_state(
                    dtype=tf.float32, batch_size=batch_size_tensor))
            # And finally perform the decode
            final_outputs, final_state, final_sequence_lengths = tf.contrib.seq2seq.dynamic_decode(
                decoder=decoder, output_time_major=True,
                impute_finished=True, maximum_iterations=self.input_steps
            )
            return final_outputs

        outputs = decode(my_helper)
        
        # Now from the slot decoder outputs, get the corresponding output word (slot label, from ids to words)
        self.decoder_prediction = self.slotEmbedder.get_words_from_indexes(tf.to_int64(outputs.sample_id))
        # make this tensor retrievable by name at test time
        self.decoder_prediction = tf.identity(self.decoder_prediction, name="decoder_prediction")
        # Get some informations on the performed decoding: the maximum number of steps done in the batch
        decoder_max_steps, _, _ = tf.unstack(tf.shape(outputs.rnn_output))


        # Losses requirements: get comparable tensors from graph and from target values

        # For slot filling
        # Now on the decoder targets (used in training only), get their ids (from words to ids)
        decoder_targets_ids = self.slotEmbedder.get_indexes_from_words_tensor(self.decoder_targets)
        # Swap the dimensions: from (batch, time) to (time, batch)
        self.decoder_targets_time_majored = tf.transpose(decoder_targets_ids, [1, 0])
        # Truncate them on the actual decoding maximum number of steps (to have same length as decoder outputs)
        self.decoder_targets_true_length = self.decoder_targets_time_majored[:decoder_max_steps]
        # Define mask so padding does not count towards loss calculation
        # TODO 0 depends on the id associated to '<PAD>'. Change it to slotEmbedder.get_id('<PAD>')
        self.mask = tf.to_float(tf.not_equal(self.decoder_targets_true_length, self.slotEmbedder.get_indexes_from_words_list(['<PAD>'])[0]))


        # Losses definitions
        # for the slots, using builtin sequence_loss
        loss_slot = tf.contrib.seq2seq.sequence_loss(
            outputs.rnn_output, self.decoder_targets_true_length, weights=self.mask)
        # For the intent, using cross entropy
        cross_entropy = tf.nn.softmax_cross_entropy_with_logits(
            labels=tf.one_hot(intent_ids_targets, depth=self.intentEmbedder.vocab_size, dtype=tf.float32),
            logits=intent_logits)
        loss_intent = tf.reduce_mean(cross_entropy)
        # Combine the losses
        if self.intent_combination == 'crf':
            self.loss = loss_slot + loss_crf
        else:
            self.loss = loss_slot + loss_intent
        optimizer = tf.train.AdamOptimizer(name="a_optimizer")
        self.grads, self.vars = zip(*optimizer.compute_gradients(self.loss))
        #print("vars for loss function: ", self.vars)
        # Clip gradients to prevent exploding ones
        self.gradients, _ = tf.clip_by_global_norm(self.grads, 5)  # clip gradients
        self.train_op = optimizer.apply_gradients(zip(self.gradients, self.vars))


    def step(self, sess, mode, train_batch):
        """do a step on the current batch"""
        if mode not in ['train', 'test']:
            print('mode is not supported', file=sys.stderr)
            sys.exit(1)
        seq_in, length, seq_out, intent = list(zip(*[(sample['words'], sample['length'], sample['slots'], sample['intent']) for sample in train_batch]))
        if self.multi_turn:
            previous_intent, bot_turn_length = list(zip(*[(sample['previous_intent'], sample['bot_turn_actual_length']) for sample in train_batch]))
        else:
            previous_intent, bot_turn_length = None, None
        #print(seq_in, length)
        #try:
        if mode == 'train':
            output_feeds = [self.train_op, self.loss, self.decoder_prediction,
                            self.intent, self.mask]
            feed_dict = {self.words_inputs: np.transpose(seq_in, [1, 0]),
                        self.encoder_inputs_actual_length: length,
                        self.decoder_targets: seq_out,
                        self.intent_targets: intent}
        if mode in ['test']:
            output_feeds = [self.decoder_prediction, self.intent]
            feed_dict = {self.words_inputs: np.transpose(seq_in, [1, 0]),
                        self.encoder_inputs_actual_length: length}
        
        if self.multi_turn:
            feed_dict.update({
                self.previous_intent: previous_intent,
                self.bot_turn_actual_length: bot_turn_length
            })

        results = sess.run(output_feeds, feed_dict=feed_dict)
        if mode in ['test']:
            slots_batch, intent_batch = results
            for idx, slots in enumerate(slots_batch):
                slots_batch[idx] = np.array([slot.decode('utf-8') for slot in slots])
            for idx, intent in enumerate(intent_batch):
                intent_batch[idx] = intent.decode('utf-8')
            results = slots_batch, intent_batch
        #except Exception as e:
        #    traceback.print_exc()
        #    print(seq_in, length)
        return results
