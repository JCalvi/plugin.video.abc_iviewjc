# ABC iView+

An unofficial ABC iView video add-on for Kodi, branded as **ABC iView+** to distinguish it from the older Aussie Add-ons plug-in.

The add-on uses a unique Kodi add-on ID, `plugin.video.abc_iviewjc`, so it can
be installed alongside the older aussieaddons `plugin.video.abc_iview` add-on.

## Features

- Browse ABC iview channels, categories, collections, shows and episodes
- Play ABC live television streams
- Link an ABC Account using the television QR-code/device-code flow
- My Watchlist
- Continue Watching
- Two-way watched-status and resume-progress sync
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
4. Open **ABC iView+** from Video add-ons.

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

- Built on the SlyGuy Kodi add-on framework, thanks to [Matt Huisman](https://github.com/matthuisman)
- Catalogue and playback behaviour informed by the earlier open-source ABC iView Kodi add-on from [Aussie Addons](https://aussieaddons.com)
- ABC Account support uses the connected-TV device-link workflow exposed by
  ABC's television application services

## Licence

The source code is distributed under the GNU General Public License,
version 3. See `LICENSE`.


## Library Integration (2.0)

Enable **Settings > Library Integration > Enable Library Integration** to create a small local Kodi TV library. Select **Shows to include** to choose **Manual selection only** (the safe default for fresh installs), **Shows with a currently available watched episode**, or **My Watchlist only**. In manual mode, use a show's context menu to add or remove it from the Kodi library selection. Removing a manually selected show through Kodi's native **Manage > Remove from library** action also clears the add-on's manual selection, so the show is not recreated by a later scan. Once qualified, the add-on imports all currently available seasons and episodes for that show, refreshes them periodically, and routes playback back through the add-on using `.strm` files.

Changing the inclusion mode re-evaluates the whole generated ABC iView library. Shows that no longer qualify are removed from disk, Kodi's library is cleaned for the ABC iView directory, and the remaining content is rescanned. Every change is handled as one transaction: files are reconciled first, followed by one scoped clean when required, one Kodi scan when required, and queued watched/resume values in one batch. Kodi's own clean/scan notifications update the interface; the add-on contains no `Container.Refresh` call or refresh retry state. Requests received during the transaction are coalesced. No Home, Favourites or library-window blocking is used.

The watched mode requires at least one currently available episode with a genuine watched/playcount state. Starting or partially watching an episode does not qualify a new show.

The generated library is stored under the add-on profile. Version 2.0 automatically adds and configures the source on Kodi installations using the normal local SQLite video database. Remote MySQL/MariaDB video databases require manual source configuration.


Library mode changes are handed to the background service through a durable on-disk request queue. The service also watches a configuration revision token, so a change is applied even if Kodi launches the settings action in a separate Python interpreter. The configuration menu includes **Apply settings and reconcile now** for an explicit full requalification, prune, clean and rescan.


## Service cadence and ToggleWatched

The background service is event-driven: Kodi notifications, playback callbacks, durable request wakes and explicit retry deadlines schedule real work. While idle it performs no library tick and never polls the selected item or Kodi's video database. A low-frequency 60-second disk check is only a safety net for an actual queued request whose immediate wake token was missed.

Playback progress is written when playback is paused, stopped or completed. A three-minute periodic fallback protects resume progress during long uninterrupted playback if Kodi or the device exits unexpectedly.

Plugin-browser Mark watched, Mark unwatched and `ToggleWatched` use Kodi's own committed plugin-URL playcount followed by Kodi's normal folder reload. The next dispatch compares the previous render manifest with the committed read-only database rows and queues the ABC change.
