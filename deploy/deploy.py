import os
import sys
from pathlib import Path
import paramiko
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

VPS_IP = os.getenv("IP_address")
VPS_LOGIN = os.getenv("LOGIN", "root")
VPS_PASSWORD = os.getenv("PASSWORD")
REMOTE_DIR = "/opt/alldlbot"

def print_step(msg: str):
    print(f"\n[+] {msg}")

def deploy():
    if not VPS_IP or not VPS_PASSWORD:
        print("[Error] IP_address and PASSWORD must be set in your .env file.")
        sys.exit(1)

    print("==================================================")
    print(" Deploying All-In-One Downloader Bot to VPS")
    print(f" Target Server: {VPS_LOGIN}@{VPS_IP}")
    print("==================================================")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        print_step("Connecting to server via SSH...")
        client.connect(VPS_IP, username=VPS_LOGIN, password=VPS_PASSWORD, timeout=15)
        print("Connected successfully!")

        print_step("Installing system packages (FFmpeg, Python3, Pip)...")
        install_cmd = (
            "apt update -y && "
            "apt install -y ffmpeg python3-pip python3-full curl && "
            "mkdir -p /opt/alldlbot"
        )
        stdin, stdout, stderr = client.exec_command(install_cmd)
        stdout.channel.recv_exit_status()

        print_step("Uploading project files to /opt/alldlbot...")
        sftp = client.open_sftp()

        # Files to upload
        files_to_upload = [
            ".env",
            "requirements.txt",
            "config.py",
            "bot.py",
        ]

        for fname in files_to_upload:
            local_file = BASE_DIR / fname
            if local_file.exists():
                remote_path = f"{REMOTE_DIR}/{fname}"
                print(f"  -> Uploading {fname}...")
                sftp.put(str(local_file), remote_path)

        # Upload folders: downloaders, utils, deploy
        for folder in ["downloaders", "utils", "deploy"]:
            local_folder = BASE_DIR / folder
            remote_folder = f"{REMOTE_DIR}/{folder}"
            try:
                sftp.mkdir(remote_folder)
            except IOError:
                pass

            for item in local_folder.iterdir():
                if item.is_file():
                    remote_file = f"{remote_folder}/{item.name}"
                    print(f"  -> Uploading {folder}/{item.name}...")
                    sftp.put(str(item), remote_file)

        sftp.close()

        print_step("Installing Python requirements on VPS...")
        pip_cmd = (
            "pip3 install --break-system-packages -r /opt/alldlbot/requirements.txt"
        )
        stdin, stdout, stderr = client.exec_command(pip_cmd)
        stdout.channel.recv_exit_status()

        print_step("Configuring Systemd service (alldlbot.service)...")
        service_cmd = (
            "cp /opt/alldlbot/deploy/alldlbot.service /etc/systemd/system/alldlbot.service && "
            "systemctl daemon-reload && "
            "systemctl enable alldlbot && "
            "systemctl restart alldlbot"
        )
        stdin, stdout, stderr = client.exec_command(service_cmd)
        stdout.channel.recv_exit_status()

        print_step("Checking service status...")
        stdin, stdout, stderr = client.exec_command("systemctl is-active alldlbot")
        status = stdout.read().decode().strip()
        print(f"Service status: {status}")

        print("\n==================================================")
        print(" DEPLOYMENT COMPLETE! ")
        print(f" Bot is now running 24/7 on VPS: {VPS_IP}")
        print(" To view live logs on server, run:")
        print("   journalctl -u alldlbot -f")
        print("==================================================")

    except Exception as e:
        print(f"\n[-] Deployment failed: {e}")
    finally:
        client.close()

if __name__ == "__main__":
    deploy()
