# Start here

This is a crypto trading bot with a web page that shows you every decision it
makes and why. **It does not touch real money.** It watches real live prices and
pretends to trade, so you can see whether it would have worked.

**You need about 5 minutes. Nothing costs anything. No account of any kind is
needed — not Robinhood, not Coinbase, nothing.**

There is one thing to install, and then two lines to paste. That is the whole
setup.

---

## Step 1 — Install Python

This is the only thing you have to install.

1. Open this page: **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button.
3. Open the file it downloads (it will be in your Downloads folder).
4. Click **Continue** / **Install** until it finishes. It will ask for your Mac
   password — that is normal.

That's it. You never have to think about Python again.

> **Already have it?** Fine — doing it again changes nothing.

---

## Step 2 — Get the app

Open the app called **Terminal**: press `Cmd + Space`, type `terminal`, press
Enter. A white or black window with text appears. That is where the next two
lines go.

Copy this whole line, paste it into Terminal, press Enter:

```
git clone https://github.com/pouriaetab/tradecrypto.git ~/tradecrypto
```

Three things may happen. All are normal:

- **A box appears asking to install developer tools** → click **Install**, wait,
  then paste the line again.
- **It asks you to sign in to GitHub** → do that.
- **It prints a few lines ending in `done.`** → it worked.

**Where did it go?** Into your **home folder** — not your Desktop. That is
correct and you do not need to find it. To see it anyway: in Finder, click
**Go → Home** in the top menu.

---

## Step 3 — Turn it on

Paste this one line and press Enter:

```
cd ~/tradecrypto && bash run.sh
```

The first time takes a few minutes — it is downloading what it needs. Leave the
window alone until it stops printing.

When it is ready, the last line will say something like:

```
TradeCrypto up — dashboard at http://127.0.0.1:8006
```

---

## Step 4 — Look at it

Open your browser and go to **the address printed in that last line**.

It is usually one of these two:

- **http://127.0.0.1:8006**
- **http://127.0.0.1:5180**

Use whichever one Terminal printed. That's it — you are running it.

The first few minutes look empty. It is collecting live prices. Within about
five minutes the **Universe** and **Movers** pages fill with real coins and real
prices.

---

## To stop it

Click on the Terminal window and press `Ctrl + C`.

Or, from any Terminal window:

```
cd ~/tradecrypto && bash run.sh stop
```

## To start it again later

```
cd ~/tradecrypto && bash run.sh
```

## To get the owner's latest changes

```
cd ~/tradecrypto && git pull && bash run.sh
```

Your own history is never touched by an update.

---

## What to know before you read the numbers

1. **It is pretending.** Real prices, fake money. Nothing can be lost.
2. **Trading costs about 1.9% per round trip.** Most coins move 2–3% in a day,
   so the bot has to beat that fee before it earns a cent. This is the hard
   part, and the app is honest about it.
3. **Your numbers start today.** It arrives with an empty history. Nothing you
   see comes from anyone else.
4. **It is a research tool**, not advice.

### Your stake

It assumes a pretend $500. To change it, open the file `.env` inside the
`tradecrypto` folder with TextEdit, find `TC_ACCOUNT_EQUITY=500`, and put your
own number. Stop and start the app afterwards.

---

## If something goes wrong

Try this first:

```
cd ~/tradecrypto && bash run.sh stop && bash run.sh
```

If it prints a red line, that line says what to do — they are written in plain
English, not error codes.

To see a full report of what is and isn't working:

```
cd ~/tradecrypto && bash run.sh --doctor
```

Send a screenshot of that. Or in the web page, click **Sources** then
**events**, which explains problems in plain language.

---

## Optional — on your phone too

Skip this unless you want it. There is no app to install; you open it in your
phone's browser.

1. Open the file `.env` in the `tradecrypto` folder with TextEdit.
2. Find the line `TC_TUNNEL=0` and change it to `TC_TUNNEL=1`. Save.
3. Install the one extra piece this needs. In Terminal:

   ```
   brew install cloudflared
   ```

   If that says `command not found: brew`, you need Homebrew first — this is
   the only genuinely technical part of the whole app, so it is fine to stop
   here and just use it on your computer.

4. Start the app again. It prints a web address ending in `.trycloudflare.com`.
   Open that on your phone. It works on cell data, anywhere.

**One rule:** the folder `secrets/` holds a password file that lets your phone
in. Never send it to anyone and never put it online.
