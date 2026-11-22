# Migration notes

This is a repository and branding split, not a change to the deployed server. All runtime paths and systemd service names remain `pi-remote`. Do not rename them while stabilising the native Android client.

The bundled source was copied from the supplied `pi-remote-v0.3.0` archive. The original `server.py`, `qr_display.py`, `install.sh`, `static/*`, tests, and CHANGELOG are unchanged. Only documentation, workflow, `.gitignore`, and the GitHub upload helper were added.
