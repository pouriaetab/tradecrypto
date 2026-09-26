# Start here

> **A research project, not a product.** This is an engineering demonstration
> built on free public crypto data. The full write-up is in
> [README.md](README.md); scope and terms are in [DISCLAIMER.md](DISCLAIMER.md).
>
> It **cannot place an order**. The broker integration was removed before
> publication. It watches real live prices and decides what it *would* do.
>
> The strategy does not clear its own cost floor: a round trip costs about 1.9%
> while these coins typically move 2–3% in a day. That is the finding, and the
> app is built to show it to you rather than hide it.


This is a crypto trading bot with a web page that shows you every decision it
makes and why. **It cannot place an order.** It watches real live prices and
decides what it would do, so you can see whether it would have worked.

**You need about 5 minutes. Nothing costs anything. You do not need an account
of any kind: not Robinhood, not Coinbase, nothing.**

There is one thing to install, and then two lines to paste. That is the whole
setup.

---

## Step 1. Install Python

This is the only thing you have to install.

1. Open this page: **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button.
3. Open the file it downloads (it will be in your Downloads folder).
4. Click **Continue** / **Install** until it finishes. It will ask for your Mac
   password, which is normal.

That's it. You never have to think about Python again.

> **Already have it?** Fine. Doing it again changes nothing.

---

## Step 2. Get the app

Open the app called **Terminal**: press `Cmd + Space`, type `terminal`, press
Enter. A white or black window with text appears. That is where the next two
lines go.

Copy this whole line, paste it into Terminal, press Enter:

```
git clone https://github.com/pouriaetab/glassbox.git ~/glassbox
```

Three things may happen. All are normal:

- **A box appears asking to install developer tools** → click **Install**, wait,
  then paste the line again.
- **It asks you to sign in to GitHub** → do that.
- **It prints a few lines ending in `done.`** → it worked.

**Where did it go?** Into your **home folder** rather than your Desktop. That is
correct and you do not need to find it. To see it anyway: in Finder, click
**Go → Home** in the top menu.

---

## Step 3. Turn it on

Paste this one line and press Enter:

```
cd ~/glassbox && bash run.sh
```

The first time takes a few minutes while it downloads what it needs. Leave the
window alone until it stops printing.

When it is ready, the last line will say something like:

```
Glassbox up. Dashboard at http://127.0.0.1:8006
```

---

## Step 4. Look at it

Open your browser and go to **the address printed in that last line**.

It is usually one of these two:

- **http://127.0.0.1:8006**
- **http://127.0.0.1:5180**

Use whichever one Terminal printed. That's it. You are running it.

The first few minutes look empty. It is collecting live prices. Within about
five minutes the **Universe** and **Movers** pages fill with real coins and real
prices.

---

## To stop it

Click on the Terminal window and press `Ctrl + C`.

Or, from any Terminal window:

```
cd ~/glassbox && bash run.sh stop
```

## To start it again later

```
cd ~/glassbox && bash run.sh
```

## To get the owner's latest changes

```
cd ~/glassbox && git pull && bash run.sh
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
`glassbox` folder with TextEdit, find `TC_ACCOUNT_EQUITY=500`, and put your
own number. Stop and start the app afterwards.

---

## If something goes wrong

Try this first:

```
cd ~/glassbox && bash run.sh stop && bash run.sh
```

If it prints a red line, that line says what to do. These messages are written
in plain English, not error codes.

To see a full report of what is and isn't working:

```
cd ~/glassbox && bash run.sh --doctor
```

Send a screenshot of that. Or in the web page, click **Sources** then
**events**, which explains problems in plain language.

---

## On your phone too

Two ways. The first needs nothing installed.

### Way 1. At home, on the same wifi (easiest)

1. Open the file `.env` inside the `glassbox` folder. Double-click it; if your
   Mac asks what to open it with, choose **TextEdit**.
2. Find the line that says `TC_LAN=0` and change the `0` to a `1`, so it reads:

   ```
   TC_LAN=1
   ```

3. Save the file and close it.
4. In Terminal, stop and start the app:

   ```
   cd ~/glassbox && bash run.sh stop && bash run.sh
   ```

5. Near the end it prints something like:

   ```
   phone access is ON, nothing to install
     On your phone, on the same wifi as this Mac, open:
         http://192.168.1.24:8006/?token=a1b2c3...
   ```

6. Type that whole address into your phone's browser. That **includes the
   `?token=...` part**. You only type it once; the phone remembers it.
7. In Safari, tap **Share -> Add to Home Screen**. It gets its own icon and
   opens like an app.

**Needs:** your phone and this Mac on the same wifi, and this Mac awake with the
app running. The phone is only a window onto the program, which runs on the Mac.

**If the page will not load:** some home wifi, and most hotel and office wifi,
blocks devices from talking to each other. Nothing in this app can fix that, so
use Way 2.

### Way 2. Anywhere, including cell data

This one needs one extra piece of software, so it is a bit more work.

1. Install Homebrew. Paste this into Terminal and press Enter; it asks for your
   Mac password and takes a few minutes:

   ```
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

2. Then:

   ```
   brew install cloudflared
   ```

3. Open `.env` in TextEdit, change `TC_TUNNEL=0` to `TC_TUNNEL=1`, save.
4. Restart the app:

   ```
   cd ~/glassbox && bash run.sh stop && bash run.sh
   ```

5. It prints an address ending in `.trycloudflare.com`. Open that on your phone.
   It works on cell data, anywhere.

That address survives app restarts. It only changes if you run
`bash run.sh --tunnel-restart`.

### One rule, either way

The folder `secrets/` holds the password file that lets your phone in. Never
send it to anyone and never put it online. Anyone with that link and token can
see your dashboard.
