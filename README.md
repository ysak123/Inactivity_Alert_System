# PD Run Monitoring Alert System
Think of this tool as a quiet control-tower assistant for physical design runs: 
- it keeps watch over remote folders,
- notices when activity slows down, and 
- sounds an alert when a run appears to be stuck.

Purpose
- This system improves the PD workflow during unpredictable place-and-route and signoff stages by reducing downtime.
 It helps the PD team monitor multiple block runs more effectively and respond sooner when progress stops.


# How it works
### Inputs
- Enter the SSH connection details: host, username, and private key.
- Select one MP3 file to use as the alert sound.
- Add one or more remote folders to monitor.
- Set a dedicated timeout value for each folder in H:M:S format.

### Process
- The app connects to SSH/SFTP in the background.
- It polls each configured folder every few seconds.
- It compares the current folder contents against the previous snapshot.
- When a file is added, removed, or modified, the app logs the change and resets that folder’s timer.
- If no change is detected, that folder’s countdown continues.

### What happens
- When a folder reaches its timeout, the selected MP3 plays. If the MP3 is already playing, it restarts from the beginning.
- Monitoring continues even after the alert sound starts.
- If the SSH connection drops, the app logs the error and automatically retries the connection.
- Pressing Stop immediately stops both monitoring and the music.
- Pressing Remonitoring starts asynchronous monitoring of the timeout for a specific path
- Closing the app saves the latest inputs to JSON and exits cleanly.

> Notes
> Each folder has its own independent timer, so active runs are not affected by inactive ones.

The tool is designed to keep monitoring in the background, like a steady metronome for run progress, while the team focuses on analysis and closure.

---

# Before Started, you need to have SSH connection configure in Windows and Enclave Environment (Linux)
 

If you configured Windows with VSCode (connected to Enclave server) [Windows Setup - VSCode and Github Copilot]
- You can skip Step 1 and Step 2 but reuse the private key directory later in the Application

1. Generate an SSH Key Pair on Windows
Open CMD or power shell and run
```
ssh-keygen -t rsa -b 4096
place in default location : C:\Users\<your_username>\.ssh\id_rsa
```

It will create two files:
- id_rsa : private key (keep this secret, never share)
- id_rsa.pub : public key (this goes on the remote server)

2. Copy Public key to Enclave Server
> here we use manual copy
Copy the content in : C:\Users\<your_username>\.ssh\id_rsa_pub
Go to the Enclave Environment (Linux)
```
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "PASTE_YOUR_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```
3. Check the SSH Connection
(Optional) In the Windows CMD:
```
ssh your_username@your-hostname.png.altera.com
```
OR you can directly open the Application and Press Start Monitoring to check the connection

---
 
# Application Interface & Usage

<img width="1935" height="1082" alt="image" src="https://github.com/user-attachments/assets/acb95c92-c72b-4dbe-a3cf-c60339d29418" />

### 1. Connection Settings
<img width="759" height="150" alt="image" src="https://github.com/user-attachments/assets/1f35ab15-3b41-4015-b6b1-b0cff9bb550b" />

SSH Host: Check your hostname on the Enclave Server (Linux)


```
hostname -f
```
SSH Username: Your username on the Enclave Server
Private Key: If you configured SSH, it should be at C:\Users\<your_username>\.ssh\id_rsa
MP3 file: Browse an mp3 sound or music long enough to alert you
- Stop the alert/music by pressing Stop Music when music is Ongoing.

 
### 2. Remote Directories Settings
Initial rows: Set the number of path directories to monitor and press Generate Rows to create the rows
<img width="1069" height="190" alt="image" src="https://github.com/user-attachments/assets/e6f94a9b-5081-443b-b1c6-19c3ab8a0407" />

- Or add a path row manually by pressing Add Directory
- Remove a row by pressing the right-most Remove button
- You are able to restart the monitoring process by pressing Remonitor button
Timeout (H:M:S): Set different timeout durations for each path
- Example: 10 minutes timeout triggers alert/music if no changes occur in the path

### 3. Start and Stop Button
<img width="203" height="35" alt="image" src="https://github.com/user-attachments/assets/2337e367-5a5a-4fb0-859d-64428d5094b7" />
Start Monitoring: Begin monitoring after completing Step 1 and 2
Stop: End the monitoring process

### 4. Countdown & Status
<img width="639" height="121" alt="image" src="https://github.com/user-attachments/assets/a2ef148d-b335-413e-9caf-66e521e3b973" />
View if the state is Timeout or Monitoring
- The Remaining column shows the timeout left


### 5. Log Message
<img width="596" height="240" alt="image" src="https://github.com/user-attachments/assets/bc6678ae-8d50-4e98-98a8-9b2cb207054f" />
Important messages log here, but no external log file is created

---

# Saved Settings
After setting the app and running monitoring,
- It generates a JSON configuration file

<img width="85" height="131" alt="image" src="https://github.com/user-attachments/assets/1508d37f-d08e-4c68-8ffd-0ffd2d01b66f" />

When you reopen the app, it preloads this JSON file.

Inside the JSON file, it looks like this:
```
{
  "host": "asccc04103707.sc.altera.com",
  "username": "ysak",
  "key_path": "C:\\Users\\ysak\\.ssh\\id_rsa",
  "mp3_path": "C:/Users/ysak/Music/alert_music.mp3",
  "directories": [
    {
      "path": "/nfs/site/disks/km6_pnr_21/users/ysak/mio_bf/26ww17/runs/dr_hseam_mio_bf/lynx_h169_m18/testing",
      "hours": "0",
      "minutes": "0",
      "seconds": "10"
    },
    {
      "path": "/nfs/site/disks/home_user/ysak/test1",
      "hours": "0",
      "minutes": "1",
      "seconds": "00"
    }
  ],
  "initial_dir_count": "2"
}
```

---
# How to Get the Application 
### 1. Directly download the Executable from above
- [notify_sys.exe](https://altera-my.sharepoint.com/:u:/r/personal/yee_liang_sak_altera_com/Documents/Microsoft%20Teams%20Chat%20Files/notifiy_sys.exe?csf=1&web=1&e=2R7fLl)

### 2. Create your own Executable file
- After copying the code from notify_sys.py, place below code into your IDE

```
pyinstaller --noconfirm --onefile --windowed notifiy_sys.py --collect-all pygame --icon "notification_bell.ico"
```

