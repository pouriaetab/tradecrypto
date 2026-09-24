# Start here

This is a crypto trading bot with a web page that shows you everything it
decides and why. **It does not touch real money.** It watches real live prices
and pretends to trade, so you can see whether it would have worked.

You need about 10 minutes. Nothing here costs anything and no account is needed.

---

## Step 1 — Get the two things it needs

You probably have neither. That is fine.

**On a Mac**, open the app called **Terminal** (press `Cmd + Space`, type
`terminal`, press Enter). Copy this whole line, paste it in, press Enter:

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

It will ask for your Mac password. Typing it shows nothing on screen — that is
normal. Press Enter when done. This takes a few minutes.

When it finishes, paste this and press Enter:

```
brew install python@3.11 node git
```

**On Windows**, install these two by hand, clicking "next" through both:
- Python: https://www.python.org/downloads/ — **tick "Add Python to PATH"** on the first screen
- Node: https://nodejs.org/ — pick the "LTS" button

---

## Step 2 — Get the app

In the same Terminal window, paste this one line and press Enter. Replace the
web address with the one you were sent:

```
git clone https://github.com/OWNER/tradecrypto.git ~/tradecrypto
```

It may ask you to sign in to GitHub. Do that.

---

## Step 3 — Turn it on

Paste these three lines, one at a time, pressing Enter after each:

```
cd ~/tradecrypto
cp .env.example .env
./run.sh
```

The first time takes a few minutes — it is downloading what it needs. Leave the
window alone until it stops printing.

---

## Step 4 — Look at it

Open your browser and go to:

**http://127.0.0.1:5180**

That is it. You are running it.

---

## To stop it

Click on the Terminal window and press `Ctrl + C`. Or:

```
cd ~/tradecrypto
./run.sh stop
```

## To start it again later

```
cd ~/tradecrypto
./run.sh
```

---

## Want it on your phone too?

There is no app to install — you open it in your phone's browser.

Open the file `.env` in the `tradecrypto` folder with TextEdit or Notepad, find
the line that says `TC_TUNNEL=0`, change it to:

```
TC_TUNNEL=1
```

Save the file. Then in Terminal:

```
brew install cloudflared
cd ~/tradecrypto
./run.sh
```

It will print a web address ending in `.trycloudflare.com`. Open that on your
phone. It works on cell data, anywhere.

**One rule:** the folder `secrets/` contains a password file that lets your
phone in. Never send it to anyone and never put it online.

---

## What to know before you read the numbers

1. **It is pretending.** Real prices, fake money. Nothing can be lost.
2. **Trading costs about 1.9% per round trip.** Most coins move 2–3% a day, so
   the bot has to beat that fee before it earns a cent. This is the hard part.
3. **Your numbers start today.** The app arrives with an empty history. Nothing
   you see comes from anyone else.
4. **It is a research tool**, not advice.

---

## If something goes wrong

Try this first:

```
cd ~/tradecrypto
./run.sh stop
./run.sh
```

Still stuck? In the web page, click **Sources** then **events** — it explains
problems in plain English. Send a screenshot of that.
