# Brand artwork

Home Assistant serves the icons from `custom_components/comelit_vip/brand/`
(`icon.png` 256x256 and `icon@2x.png` 512x512); since 2026.3 a local `brand/`
directory takes precedence over the brands CDN, and `logo*.png` falls back to
`icon*.png`.

`logo@2x.png` (1024x512) is kept here rather than shipped: it is the right size
for the hDPI logo but the wrong size for `logo.png`, whose shortest side must be
128 to 256 pixels, so shipping it alone would mix the wordmark and the icon in
one slot.

The artwork is original, a generic intercom panel, and carries no Comelit logo
or wordmark.
