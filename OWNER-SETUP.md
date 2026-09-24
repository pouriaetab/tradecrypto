# Owner: putting this on GitHub

For the person sharing the app, not the person receiving it. Do this once.

**Run every command in Terminal, from the folder this file is in.**

---

## Before you start

Check that git is installed. Paste this:

```
git --version
```

If it says "command not found", paste:

```
brew install git
```

---

## Step 1 — Make this folder a repository

```
cd /path/to/collab/tradecrypto
git init -b main
```

---

## Step 2 — CHECK NOTHING PRIVATE IS ABOUT TO BE SENT

**Do not skip this.** Paste all of it and press Enter:

```
git add -A
git ls-files | grep -iE "\.env$|secrets/|\.sqlite|\.log$|/data/"
```

**It must print nothing at all.**

If it prints anything, stop and ask before continuing — something private is
staged. Nothing has left your machine yet, so it is fixable.

---

## Step 3 — Save the first version

```
git commit -m "TradeCrypto"
```

---

## Step 4 — Create the repository on GitHub

1. Go to **https://github.com/new**
2. **Repository name:** `tradecrypto`
3. **Description:** leave blank
4. Choose **Private** ← important
5. Do **not** tick "Add a README", "Add .gitignore" or "Choose a license" — all
   three must stay unticked or the next step fails
6. Click **Create repository**

GitHub then shows a page with commands. Ignore it and use the next step.

---

## Step 5 — Send your code up

Replace `YOURNAME` with your GitHub username:

```
git remote add origin https://github.com/YOURNAME/tradecrypto.git
git push -u origin main
```

It will ask you to sign in. A browser window opens — approve it there.

Refresh the GitHub page. Your code is there, and only you can see it.

---

## Step 6 — Add your friend

1. On your repository page, click **Settings** (top right)
2. In the left sidebar, click **Collaborators**
3. Click **Add people**
4. Type his GitHub username or the email he signed up with
5. Click **Add**

He gets an email invitation. He must accept it before he can download anything.

**What to ask him for:** his GitHub username — that is all. If he does not have
an account, he makes one free at https://github.com/signup and it takes two
minutes.

---

## Step 7 — Send him the instructions

Tell him to read **START-HERE.md** in the repository, and give him this address:

```
https://github.com/YOURNAME/tradecrypto
```

---

## Sending him a change later

Every time you want him to have your latest version:

```
cd /path/to/collab/tradecrypto
git add -A
git commit -m "what changed, in a few words"
git push
```

Then tell him to run the two lines in **HOW-TO-UPDATE.md**.

**Run the check from Step 2 before every push**, not just the first one:

```
git ls-files | grep -iE "\.env$|secrets/|\.sqlite|\.log$|/data/"
```

Still must print nothing.

---

## If you want to make it public one day

Repository page → **Settings** → scroll to the bottom → **Change visibility** →
**Make public**.

Two things to understand before you do:

1. **Everything you have ever pushed becomes visible**, including older versions.
   If a private file was ever pushed by accident, making the repo public exposes
   it even if you deleted it later. The Step 2 check is what prevents this.
2. **Anyone can copy it.** That is usually fine. Decide on purpose.
