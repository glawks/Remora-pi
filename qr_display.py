#!/usr/bin/env python3
"""Show a temporary QR pairing window on the Pi desktop.

It is deliberately independent of the remote control service: the window
exits as soon as a newly paired phone authenticates its WebSocket, and it is
skipped on later logins once a trusted phone exists.
"""
from __future__ import annotations

import argparse
import base64
from io import BytesIO
import os
from pathlib import Path
import re
import socket
import sys
import time
from urllib.parse import urlsplit

from server import (
    DATA_DIR, db_connect, digest, has_new_connected_device,
    issue_pairing_code, newest_device_id, paired_count,
)


def make_pair_url(base_url: str, code: str) -> str:
    """Use a fragment: scanners open the page without sending the code in GET logs."""
    url = base_url.rstrip('/')
    parts = urlsplit(url)
    if (parts.scheme not in ('http', 'https') or not parts.hostname or
            parts.username or parts.password or parts.query or parts.fragment or
            parts.path not in ('', '/')):
        raise ValueError('URL must be http(s)://pi-hostname:3100 with no path, credentials or query')
    if not re.fullmatch(r'\d{8}', code):
        raise ValueError('Invalid code')
    return url + '/#pair=' + code


def default_url() -> str:
    custom = os.environ.get('PI_REMOTE_URL')
    if custom:
        return custom
    name = socket.gethostname().split('.')[0].lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name):
        raise ValueError('Unusual hostname; set PI_REMOTE_URL=http://YOUR-PI-IP:3100')
    return f'http://{name}.local:3100'


def qr_photo(tk, url: str):
    # Pillow/QR rendering lives only in this optional desktop utility.
    import qrcode
    qr = qrcode.QRCode(border=3, box_size=8, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='#101827', back_color='white').convert('RGB')
    out = BytesIO()
    img.save(out, format='PNG')
    return tk.PhotoImage(data=base64.b64encode(out.getvalue()))


def main() -> int:
    parser = argparse.ArgumentParser(description='Show one-time Pi Remote QR code on this desktop')
    parser.add_argument('--force', action='store_true', help='Pair an additional phone even when one is already trusted')
    parser.add_argument('--url', default=None, help='Override phone-facing URL, e.g. http://192.168.1.20:3100')
    args = parser.parse_args()
    try:
        url = args.url or default_url()
        make_pair_url(url, '00000000')
    except ValueError as exc:
        print(f'Invalid QR address: {exc}', file=sys.stderr)
        return 2

    # Prevent duplicate popups if a login hook and menu launcher both run.
    import fcntl
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(DATA_DIR / 'qr-display.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return 0

    conn = None
    active_hash = None
    try:
        conn = db_connect()
        if not args.force and paired_count(conn):
            return 0
        baseline_id = newest_device_id(conn)
        # If this fails before creating Tk, don't overwrite the previously
        # issued code (e.g. a user is pairing from an SSH terminal).
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:
            print(f'Unable to open the Pi desktop pairing window: {exc}', file=sys.stderr)
            print('Run this from the graphical Pi desktop; for SSH use server.py pair.', file=sys.stderr)
            return 1

        root.title('Pi Remote · Pair phone')
        root.configure(bg='#101827')
        root.resizable(False, False)
        root.attributes('-topmost', True)
        root.geometry('430x570')

        card = tk.Frame(root, bg='#101827', padx=30, pady=18)
        card.pack(fill='both', expand=True)
        tk.Label(card, text='Pi Remote', font=('DejaVu Sans', 23, 'bold'),
                 fg='white', bg='#101827').pack(pady=(3, 4))
        tk.Label(card, text='Scan to pair your phone', font=('DejaVu Sans', 13),
                 fg='#aabbcc', bg='#101827').pack(pady=(0, 17))
        img_label = tk.Label(card, bg='white', bd=7, relief='flat')
        img_label.pack(pady=0)
        tk.Label(card, text='Or enter this code in Pi Remote:',
                 font=('DejaVu Sans', 11), fg='#aabbcc', bg='#101827').pack(pady=(12, 5))
        code_label = tk.Label(card, font=('DejaVu Sans Mono', 22, 'bold'),
                              fg='#45e1ae', bg='#101827')
        code_label.pack()
        expiration_label = tk.Label(card, text='', font=('DejaVu Sans', 10),
                                    fg='#91a5c4', bg='#101827')
        expiration_label.pack(pady=(9, 6))
        tk.Label(card, text='Closes when the new phone connects.\n'
                            'Trusted phones reconnect without a new QR code.',
                 justify='center', font=('DejaVu Sans', 10),
                 fg='#93a9c6', bg='#101827').pack(pady=(6, 9))
        closed = False
        code = ''
        expires = 0

        def update_code():
            nonlocal code, expires, active_hash
            code, expires = issue_pairing_code(conn)
            active_hash = digest(code)
            img_label.image = qr_photo(tk, make_pair_url(url, code))
            img_label.configure(image=img_label.image)
            code_label.configure(text=f'{code[:4]}  {code[4:]}')

        def close():
            nonlocal closed
            if closed:
                return
            closed = True
            # A dismissed popup must not leave a live code available for abuse.
            # Delete only its code (not another process's newer code).
            if active_hash:
                conn.execute('DELETE FROM pairing WHERE code_hash=?', (active_hash,))
                conn.commit()
            root.destroy()

        tk.Button(card, text='Close', command=close, bg='#283955', fg='white',
                  activebackground='#3d537b', activeforeground='white',
                  font=('DejaVu Sans', 11), padx=25, pady=8, borderwidth=0).pack()
        root.protocol('WM_DELETE_WINDOW', close)

        def poll():
            if closed:
                return
            if has_new_connected_device(conn, baseline_id):
                close()
                return
            # Codes rotate while waiting; old QR codes never remain valid forever.
            if time.time() >= expires:
                update_code()
            expiration_label.configure(text=f'Pairing code expires in {max(0, expires - int(time.time())) // 60}m '
                                            f'{max(0, expires - int(time.time())) % 60:02d}s')
            root.after(900, poll)

        try:
            update_code()
            poll()
            root.mainloop()
        finally:
            if not closed:
                close()
        return 0
    finally:
        if conn is not None:
            conn.close()
        os.close(lock_fd)


if __name__ == '__main__':
    raise SystemExit(main())
