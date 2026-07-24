# Fonts for PNG schedule rendering / Шрифты для рендера PNG-графика

`DejaVuSans.ttf` + `DejaVuSans-Bold.ttf` **ship with the repository** (free
license) — the PNG renderer works out of the box, no setup needed. The loader
in `app/services/image_render.py` picks fonts up by priority:

1. `DejaVuSans.ttf` + `DejaVuSans-Bold.ttf` — bundled, used by default
   (on Linux you may also `apt install fonts-dejavu-core`)
2. `arial.ttf` + `arialbd.ttf` — **optional**, not in the repo (Arial is
   proprietary and git-ignored). Copy from `C:\Windows\Fonts\` if you prefer it.

If no font file is found the renderer raises a clear error listing the
expected files — it never silently falls back to an unreadable bitmap font.

---

`DejaVuSans.ttf` + `DejaVuSans-Bold.ttf` **входят в репозиторий** (свободная
лицензия) — рендер PNG работает сразу, настройка не нужна. Загрузчик берёт
шрифты по приоритету: сначала DejaVu (в комплекте), затем Arial
(`arial.ttf`/`arialbd.ttf`) — он **опционален**, в git не входит
(проприетарный), при желании скопируйте из `C:\Windows\Fonts\`. Если шрифтов
нет вообще — понятная ошибка со списком ожидаемых файлов, а не тихий
нечитаемый битмап.
