# vmax-lcd

Show images on an "HL VMAX" PC-case LCD (USB `33c3:f101`) from Linux.

## One-time setup

```bash
sudo cp 70-vmax-lcd.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=tty
```

The rule gives the logged-in user access to the panel's serial port and stops ModemManager from probing it. The `70-` prefix matters: the rule must sort before `73-seat-late.rules`.

## Adding images

Put pictures in `image/<WIDTH>x<HEIGHT>/`, matching your panel's resolution:

```
image/
└── 320x1480/
    ├── test-pattern.png
    └── my-wallpaper.jpg
```

Run `./vmax_lcd.py info` to see your panel's resolution. It also creates the folder. Images at exactly that size look best. Others are cropped to fill (`cover`), letterboxed (`contain`) or stretched (`stretch`). Supported formats: PNG, JPEG, WebP, BMP, GIF (first frame only) and TIFF.

## Graphical picker

```bash
./vmax_lcd.py ui
```

The picker shows a grid of the images for your panel. Click one and it appears on the screen.

- **+** copies images into the folder.
- **Folder** opens the folder in your file manager.
- **Refresh** reloads the grid. The grid also refreshes by itself when files change.
- **Fit** and **Rotate** set how images are placed on the screen. Use 180° if the panel is mounted upside down.
- **Pause** stops sending, so the panel goes back to its boot animation.

The image stays on the screen while the window is open.

## Command line

```bash
./vmax_lcd.py info                     # model, resolution, image folder
./vmax_lcd.py list                     # images for this panel (* = last used)
./vmax_lcd.py show                     # last-used image (or first in folder)
./vmax_lcd.py show my-wallpaper.jpg    # by name from the folder
./vmax_lcd.py show ~/any/path.png      # any file
./vmax_lcd.py show --fit contain --rotate 180
```

`show` keeps running and resends the frame every second (Ctrl+C to stop). The panel has no on/off command. It shows its built-in boot animation whenever no host is sending frames. Only one program can use the panel at a time, so close the picker before using `show`, and stop `show` before opening the picker.

The last image picked, and the picker's fit and rotation settings, are saved in `~/.config/vmax-lcd/state.json`.

`brightness <0-100>` uses a command only one project documented (`AA BB <v> CC DD`). It may do nothing on your model.

## Protocol

Handshake `F0 A5 5A 0F` → ASCII reply such as `VMAXC170320*1480S254200198` (model, width `*` height, serial). Each frame is a bare baseline 4:2:0 JPEG at the native resolution, resent at least every ~1 s. Sources: knupson/solarmax-cm-b600l-panel, habibrehmansg/infopanel PR #152, visiuun/vdisplay.
