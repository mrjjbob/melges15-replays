# Fleet uploader (no GitHub account needed)

Sailors get their `.vkx` files (Vakaros) or `.fit` files (GPS watch) onto the site in either of two ways:

- **Email**, straight from the Vakaros Connect share button: Share → Mail → send to the fleet address, with the sail number in the subject. New mail is checked every 5 minutes, the file is added to `logs/`, and the sender gets a reply.
- **Upload page**: a simple web page where they pick their boat and the file.

Both run as a small Google Apps Script on a Gmail account set up for the fleet. It is free and needs no server. The script holds a GitHub token that can only change the files in this repository.

## Set it up (about 15 minutes, once)

1. **Pick the address sailors send to.** Either:
   - a Gmail account just for the fleet, for example `cyc.melges15.uploads@gmail.com`, or
   - **your own Gmail with a "+" address**, for example `you+melges15@gmail.com`. Mail to it arrives in your normal inbox, and with `MAIL_TO` set (step 4) the script only ever touches mail sent to that address. Use this if Google will not let you make another account. Do every step below signed in to your own Gmail.

2. **Make a GitHub token** (signed in as the repository owner): github.com → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → Generate new token.
   - Repository access: **Only select repositories** → `melges15-replays`
   - Permissions → Repository permissions → **Contents: Read and write**
   - Expiration: up to a year (put a reminder in your calendar to renew it)
   - Copy the token.

3. **Create the script** (signed in to the Gmail from step 1): go to script.google.com → **New project**, and name it "Fleet uploader".
   - Replace everything in `Code.gs` with this folder's `Code.gs`.
   - **+ → HTML**, name it `Upload` exactly, and paste in this folder's `Upload.html`.

4. **Settings**: ⚙ Project Settings → Script properties → add:

   | Property | Value |
   |---|---|
   | `GITHUB_TOKEN` | the token from step 2 |
   | `REPO` | `mrjjbob/melges15-replays` |
   | `BRANCH` | `main` |
   | `SITE_URL` | `https://mrjjbob.github.io/melges15-replays/` |
   | `UPLOAD_PASSWORD` | a fleet password for the upload page (optional) |
   | `MAIL_TO` | only with your own Gmail: the "+" address, e.g. `you+melges15@gmail.com` |

5. **Turn on email checking**: back in the editor choose the `setup` function and press **Run**. Google asks for permission. Because this is your own script it says "Google hasn't verified this app": choose **Advanced → Go to Fleet uploader** and **Allow**.

6. **Publish the upload page**: **Deploy → New deployment** → type **Web app**. Set Execute as **Me** and Who has access **Anyone**, then press **Deploy** and copy the web app URL.

7. **Point the site at it**: add to `site.json`:

   ```json
   "upload_url": "https://script.google.com/macros/s/…/exec",
   "upload_email": "cyc.melges15.uploads@gmail.com"
   ```

   The "Add your race" box on the site then shows the email steps, with the upload page as a second option.

## How files are matched to boats

The sail number from the subject line (email) or the boat picked on the page is put at the front of the file name, for example `302_Rubel__Bob_s_Vakaros_11-8-2026.vkx`, and the site matches boats by sail number. Without a sail number the file keeps the name Vakaros gave it, which usually carries the owner's name and is matched by surname.

A file that is already there is not added twice. Anything that is not a `.vkx`, `.fit` or `.zip` file is turned away.

**GPS watches.** A `.fit` file (or the `.zip` Garmin Connect's **Export File** saves, whose `.fit` files are taken out and added) is named by Garmin, for example `25046482066_ACTIVITY.fit`, and says nothing about the boat. So a watch file is only accepted with a sail number in the subject or a boat picked on the page, and is saved as `900_Hafen__25046482066_ACTIVITY.fit`. One file per race is fine: the site joins a boat's files for the day.

## Updating the script

The script runs from the copy pasted into script.google.com, not from this folder. After `Code.gs` or `Upload.html` changes here, paste both into the editor again, save, then **Deploy → Manage deployments → ✎ (edit) → Version: New version → Deploy**. The web app URL stays the same.

## If something goes wrong

- In the script editor, **Executions** lists every mail check and upload, with any error.
- "GitHub said 401": the token has expired or was pasted wrong. Make a new one (step 2) and update `GITHUB_TOKEN`.
- To stop it: Triggers (clock icon) → delete the `checkMail` trigger, and Deploy → Manage deployments → archive the web app.

Tip for the "+" address: in Gmail make a filter for mail to `you+melges15@gmail.com` → Skip the Inbox and apply a label "Fleet uploads". The script still finds it, and your inbox stays clean.
