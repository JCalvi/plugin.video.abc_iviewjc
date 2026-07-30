# ABC iview JC

An unofficial ABC iview video add-on for Kodi.

The add-on uses a unique Kodi add-on ID, `plugin.video.abc_iviewjc`, so it can
be installed alongside the older `plugin.video.abc_iview` add-on.

## Features

- Browse ABC iview channels, categories, collections, shows and episodes
- Play ABC live television streams
- Link an ABC Account using the television QR-code/device-code flow
- My Watchlist
- Continue Watching
- Search
- HLS playback and WebVTT subtitles where supplied by ABC
- Optional Kodi bookmarks through the SlyGuy framework

## Requirements

- Kodi 21 or later is recommended
- `script.module.slyguy` version 0.86.64 or later
- Internet access from Australia, subject to ABC availability and geographic restrictions
- A free ABC Account for Watchlist and Continue Watching

## Installation

1. Install the SlyGuy repository or otherwise install `script.module.slyguy`.
2. In Kodi, open **Add-ons → Install from zip file**.
3. Select the release ZIP containing the `plugin.video.abc_iviewjc` folder.
4. Open **ABC iview JC** from Video add-ons.

## Account linking

Select **Login** in the add-on. Kodi displays a QR code and linking code.
Open the shown ABC device-link page on a phone or computer, sign in to the
ABC Account, and enter or confirm the code.

The add-on does not ask Kodi users to enter or store their ABC password.

## Main menu

- **My Watchlist** — saved shows for the linked account
- **Continue Watching** — recently watched programmes for the linked account
- **Watch Live** — live ABC channels
- **ABC TV** and the other channel entries — ABC's editorial catalogue sections

## Privacy

The add-on stores the linked-device identifier, ABC account UID and temporary
access-token information in Kodi's add-on data through the SlyGuy framework.
Logging out requests device unlinking and removes the stored account tokens.

## Disclaimer

This is an unofficial community add-on. It is not affiliated with, sponsored
by, or endorsed by the Australian Broadcasting Corporation. ABC, ABC iview,
their names, logos, imagery and programme material remain the property of
their respective owners. Availability and APIs may change without notice.

## Credits

- Built on the SlyGuy Kodi add-on framework
- Catalogue and playback behaviour informed by the earlier open-source ABC
  iview Kodi add-on
- ABC Account support uses the connected-TV device-link workflow exposed by
  ABC's television application services

## Licence

The source code is distributed under the GNU General Public License,
version 3. See `LICENSE`.
