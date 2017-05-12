# BotCycle

BotCycle offers personalized recommendations of bike availabilities and contextual information of a city through a natural interaction via a chatbot.  

## Required libraries

This repository requires python 3

```bash
sudo apt install python3-pip python3-venv libgeos-dev
export LC_ALL=C # locale
sudo pip3 install virtualenv
```

## Setting up the environment

1. Clone/download this repository
2. Install the dependencies:
  - `virtualenv venv`
  - `source venv/bin/activate`
  - `pip install -r requirements.txt`

## Get required tokens

### Telegram tokens

In order to authenticate the bot on Telegram, it is required to ask to [BotFather](https://telegram.me/BotFather) a token and place it into a file named `tokens.json` as in the following:

```json
{
  "telegram": "PUT HERE YOUR TELEGRAM TOKEN"
}
```

### Wit.ai token

The token for `wit.ai` is linked to the online model. You can create your own token but the classifier that is used has been configured online (entities and intent) and trained with some sentences.

An *export data* exists on `wit.ai` in order to download all the data (expressions and entities). Can be useful.

The `wit.ai` token has to be stored in the `tokens.json` file as for the telegram one.

## Running the bot

Launch the bot: `python botcycle/botcycle.py tokens.json`

## Using the bot

The bot understands three intents:

- search a bike
- search an empty slot
- plan a trip
- set the user position (useful for other intents with missing entities)

The position can be sent as an attachment in any moment, also when the bot does not make request.
