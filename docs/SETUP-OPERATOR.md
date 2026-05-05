# Operator-side requirements

The "From your computer" install command in the WildWatch UI runs on your
laptop, discovers the Raspberry Pi on the local network, copies the code over
SSH, and triggers the enrollment. To make that work you need the following
tools available locally.

| Tool    | Why                                                              |
|---------|------------------------------------------------------------------|
| `bash`  | The bootstrap script (`install.sh`) is bash.                     |
| `git`   | Cloning the WildWatch repo into a temp directory.                |
| `uv`    | Running the Python orchestrator (`install_wildwatch.py`).        |
| `rsync` | Copying the capture-client source onto the RPi.                  |
| `ssh`   | Reaching the RPi (`dietpi@dietpi.local` by default).             |

You also need an SSH key set up for `dietpi@<host>` -- the orchestrator prints
the exact `ssh-copy-id` command to run if it cannot connect.

## macOS

`bash`, `git`, `rsync` and `ssh` are bundled with macOS. Only `uv` needs to be
installed:

```bash
brew install uv
```

## Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y bash git rsync openssh-client
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## Windows

The bootstrap relies on `bash` and `rsync`, which Git Bash does not ship.
The simplest path is **WSL** (Windows Subsystem for Linux):

```powershell
wsl --install -d Ubuntu
```

Then open the Ubuntu shell and follow the **Ubuntu / Debian** instructions
above. The install command from the UI is run inside WSL, not in PowerShell
or `cmd`.

## Troubleshooting

- `x uv not found in PATH`: re-open your terminal after installing `uv` so
  the new PATH entry is picked up.
- `Permission denied (publickey)`: run `ssh-copy-id dietpi@dietpi.local`
  once, then re-run the install command.
- Stuck behind a corporate proxy that blocks GitHub raw URLs: clone the
  repo manually and run `bash _recovery/install.sh --server <url>` from the
  clone instead of the `curl | bash` form.
