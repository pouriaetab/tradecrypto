# How to get updates

When the owner changes something, you get it with **two lines**.

---

## The two lines

Open Terminal and paste these, one at a time:

```
cd ~/glassbox
git pull
```

Then restart the app:

```
./run.sh stop
./run.sh
```

Refresh your browser tab. Done.

---

## What an update can and cannot touch

| | |
|---|---|
| **Changes** | the program's code |
| **Never changes** | your settings (`.env`), your trading history, your database, your phone token |

Your numbers are yours. An update cannot reach them — they are not part of what
gets sent.

---

## If `git pull` complains

You will almost certainly never see this. If you do, it means you edited a file
that the update also changed. The safe fix, which keeps your settings and your
data:

```
cd ~/glassbox
git stash
git pull
```

If it still complains, send a screenshot. Do not delete the folder — your
trading history is inside it.

---

## How to know an update is waiting

```
cd ~/glassbox
git fetch
git status
```

If it says **"Your branch is behind"**, there is an update. Run the two lines at
the top.

If it says **"Your branch is up to date"**, you already have everything.
