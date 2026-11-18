# LocalSend for the Omarchy bar

Bar widget for [LocalSend](https://localsend.org). It lists the devices on
the network that accept files, sends them files or the clipboard from the
bar, keeps favorites, and switches this machine's receiver on and off.

## Use

| Where | Action |
|---|---|
| Bar icon, left click | open the panel |
| Bar icon, right click | receiver on or off |
| Bar icon, middle click | scan again |
| Row: paper plane | pick files and send them |
| Row: clipboard | send the clipboard (text, or an image as PNG) |
| Row: star | keep the device as a favorite, shown even when offline |
| Hero switch | start LocalSend hidden in the tray, or stop it |

Keys inside the panel: `j`/`k` move, `Enter` or `s` send files, `c` send the
clipboard, `f` favorite, `x` unfavorite, `r` scan again, `t` receiver on or
off, `o` open LocalSend, `Esc` close.

The icon is bright while this machine receives and dim while it does not.
During a transfer it shows the percentage.

## How it works

`bin/localsend-helper.py` speaks LocalSend protocol v2 with the Python
standard library. Discovery announces on the multicast group and listens
for answers. At the same time it asks every host of the own /24 network for
its info, so devices that miss multicast still turn up. Sending goes through
`prepare-upload` and `upload`. Clipboard text is sent as a LocalSend text
message. The helper identifies itself with a self-signed certificate in
`~/.local/state/mae-localsend/`, which LocalSend 2.2 peers require.

The receiver is the LocalSend app itself, started as the user unit
`mae-localsend-receiver` with `--hidden`. Received files go to the folder
the app is set to save to.

Favorites are stored in the widget's entry in `~/.config/omarchy/shell.json`.

## Settings

| Key | Default | Meaning |
|---|---|---|
| `scanIntervalSec` | 20 | how often the panel rescans while it is open |

## Not covered

The panel shows an error for receivers that require a PIN. It cannot enter
one. The LocalSend app handles incoming transfers; the panel does not show
them.

## Tests

```
node --test tests/
```
