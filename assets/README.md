# Fonts for PNG schedule rendering / Шрифты для рендера PNG-графика

Fonts are not shipped with the repo (proprietary fonts must not be
redistributed). Put any of the following here — the loader in
`app/services/image_render.py` picks them up by priority:

1. `DejaVuSans.ttf` + `DejaVuSans-Bold.ttf` — free, recommended
   (Linux: `apt install fonts-dejavu-core`, files live in
   `/usr/share/fonts/truetype/dejavu/`)
2. `arial.ttf` + `arialbd.ttf` — on Windows copy from `C:\Windows\Fonts\`

Without fonts Pillow's built-in bitmap font is used (Cyrillic rendering
quality degrades).

---

Шрифты в репозиторий не входят (проприетарные распространять нельзя).
Положите сюда любой из вариантов — загрузчик подхватит по приоритету:
DejaVuSans (рекомендуется, свободный) или Arial из `C:\Windows\Fonts\`.
Без шрифтов используется встроенный шрифт Pillow (кириллица хуже).
