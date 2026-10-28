# Repo assets

`social-preview.png` (1280×640) is the card GitHub shows when the repo is shared on X, LinkedIn,
Slack or Discord. **It is not set by git** — upload it once at
*Settings → General → Social preview → Upload an image*.

Two sources are kept so it can be regenerated either way:

```bash
# ffmpeg + libass (works headless, uses the project's own fonts)
ffmpeg -y -f lavfi -i "gradients=s=1280x640:c0=0x0E5AA8:c1=0x001F3F:c2=0x00142B:nb_colors=3:x0=1150:y0=0:x1=120:y1=640:d=1" \
  -vf "format=rgb24,subtitles=.github/social-preview.ass:fontsdir=assets/fonts" \
  -frames:v 1 .github/social-preview.png

# or the HTML version, for a richer card (gradient glows, emoji)
chrome --headless=new --window-size=1280,640 \
  --screenshot=.github/social-preview.png .github/social-preview.html
```
