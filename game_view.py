"""2D top-down world rendered in an HTML canvas via Streamlit Components v2.

The canvas handles movement and collision. Mission starts are reported to Python
through a trigger so the existing scenario engine can take over.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import streamlit.components.v2 as st_components

from game_engine import CONTENT_DIR, ContentError

WORLD_PATH = CONTENT_DIR / "world.json"


@dataclass(frozen=True)
class WorldTerminal:
    code: str
    mission_id: str
    label: str
    short: str
    hint: str
    x: float
    y: float


@dataclass(frozen=True)
class WorldMap:
    title: str
    tile_size: int
    player_speed: float
    spawn: tuple[float, float]
    width: int
    height: int
    tiles: tuple[str, ...]
    terminals: dict[str, WorldTerminal]
    rooms: tuple[dict[str, Any], ...]

    def tile_at(self, tx: int, ty: int) -> str:
        if tx < 0 or ty < 0 or tx >= self.width or ty >= self.height:
            return "#"
        return self.tiles[ty * self.width + tx]

    def is_blocked(self, tx: int, ty: int) -> bool:
        return self.tile_at(tx, ty) in {"#", " ", "C", "B", "1", "2", "3", "4", "5", "6", "D"}

    def spawn_near(self, mission_id: str) -> tuple[float, float]:
        for terminal in self.terminals.values():
            if terminal.mission_id != mission_id:
                continue
            for dx, dy in ((0.0, 1.15), (0.0, -1.15), (1.15, 0.0), (-1.15, 0.0)):
                x, y = terminal.x + dx, terminal.y + dy
                if not self.is_blocked(int(x), int(y)):
                    return x, y
            return terminal.x, terminal.y + 1.15
        return self.spawn


def _parse_world(raw: dict[str, Any]) -> WorldMap:
    rows = [str(r) for r in raw["rows"]]
    if not rows:
        raise ContentError("Карта мира пуста.")
    width = len(rows[0])
    if any(len(r) != width for r in rows):
        raise ContentError("Все строки карты должны быть одинаковой длины.")
    height = len(rows)
    tiles = tuple("".join(rows))
    terminals: dict[str, WorldTerminal] = {}
    for code, meta in raw.get("terminals", {}).items():
        positions = [
            (x + 0.5, y + 0.5)
            for y, row in enumerate(rows)
            for x, ch in enumerate(row)
            if ch == code
        ]
        if len(positions) != 1:
            raise ContentError(f"Терминал '{code}' должен встречаться ровно один раз.")
        x, y = positions[0]
        terminals[code] = WorldTerminal(
            code=code,
            mission_id=str(meta["mission_id"]),
            label=str(meta["label"]),
            short=str(meta.get("short", meta["label"])),
            hint=str(meta.get("hint", "")),
            x=x,
            y=y,
        )
    spawn_raw = raw.get("spawn", {"x": 1.5, "y": 1.5})
    return WorldMap(
        title=str(raw.get("title", "CyberQuest")),
        tile_size=int(raw.get("tile_size", 32)),
        player_speed=float(raw.get("player_speed", 2.4)),
        spawn=(float(spawn_raw["x"]), float(spawn_raw["y"])),
        width=width,
        height=height,
        tiles=tiles,
        terminals=terminals,
        rooms=tuple(raw.get("rooms", [])),
    )


@lru_cache(maxsize=1)
def load_world(path: str | None = None) -> WorldMap:
    world_path = Path(path) if path else WORLD_PATH
    try:
        raw = json.loads(world_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContentError(f"Не удалось прочитать карту мира: {exc}") from exc
    return _parse_world(raw)


def validate_world(world: WorldMap, mission_ids: set[str]) -> list[str]:
    errors: list[str] = []
    mapped = {t.mission_id for t in world.terminals.values()}
    missing = mission_ids - mapped
    extra = mapped - mission_ids
    if missing:
        errors.append(f"На карте нет терминалов для миссий: {sorted(missing)}.")
    if extra:
        errors.append(f"На карте есть неизвестные миссии: {sorted(extra)}.")
    if len({t.mission_id for t in world.terminals.values()}) != len(world.terminals):
        errors.append("Несколько терминалов ссылаются на одну миссию.")
    sx, sy = int(world.spawn[0]), int(world.spawn[1])
    if world.is_blocked(sx, sy) and world.tile_at(sx, sy) not in set("S."):
        # Spawn tile itself may be 'S' which we treat as floor for collision later.
        pass
    if world.tile_at(sx, sy) == "#":
        errors.append("Точка появления находится в стене.")
    return errors


# --------------------------------------------------------------------------- #
# Canvas component
# --------------------------------------------------------------------------- #

_WORLD_HTML = """
<div class="cq-shell">
  <div class="cq-toolbar">
    <div class="cq-status" id="cq-status">Загрузка карты…</div>
    <div class="cq-keys">W A S D / стрелки — ход · E — взаимодействие · клик по карте — идти</div>
  </div>
  <canvas id="cq-canvas" tabindex="0" aria-label="Игровое поле CyberQuest"></canvas>
  <div class="cq-pad">
    <button type="button" data-dir="up" aria-label="Вверх">↑</button>
    <div class="cq-pad-mid">
      <button type="button" data-dir="left" aria-label="Влево">←</button>
      <button type="button" data-dir="down" aria-label="Вниз">↓</button>
      <button type="button" data-dir="right" aria-label="Вправо">→</button>
    </div>
    <button type="button" id="cq-interact" class="cq-interact">Взаимодействовать (E)</button>
  </div>
</div>
"""

_WORLD_CSS = """
:host, .cq-shell {
  --bg: #10151D;
  --surface: #18212D;
  --text: #E8EDF4;
  --muted: #A2ADBD;
  --border: #2B3645;
  --accent: #4C8DFF;
  --ok: #4BAE7C;
  --warn: #D7A64B;
  font-family: "Segoe UI", Arial, sans-serif;
  color: var(--text);
}
.cq-shell {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  overflow: hidden;
}
.cq-toolbar {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  background: #141b24;
  font-size: 13px;
}
.cq-status { color: var(--text); font-weight: 600; }
.cq-keys { color: var(--muted); text-align: right; }
#cq-canvas {
  display: block;
  width: 100%;
  background: var(--bg);
  outline: none;
  cursor: crosshair;
}
.cq-pad {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: #141b24;
}
.cq-pad button {
  background: #1d2733;
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 4px;
  min-width: 42px;
  min-height: 36px;
  cursor: pointer;
  font-size: 16px;
}
.cq-pad button:hover { border-color: var(--accent); color: var(--accent); }
.cq-pad-mid { display: flex; gap: 6px; }
.cq-interact {
  background: #1a2a44 !important;
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  padding: 0 14px;
  font-size: 13px !important;
  font-weight: 600;
}
.cq-interact:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
@media (max-width: 700px) {
  .cq-toolbar { flex-direction: column; }
  .cq-keys { text-align: left; }
}
"""

_WORLD_JS = r"""
export default function(component) {
  const { data, setTriggerValue, parentElement } = component;
  const root = parentElement.querySelector('.cq-shell');
  const canvas = root.querySelector('#cq-canvas');
  const statusEl = root.querySelector('#cq-status');
  const interactBtn = root.querySelector('#cq-interact');
  const ctx = canvas.getContext('2d');

  const COLORS = {
    bg: '#10151D',
    floor: '#16202B',
    floorAlt: '#182430',
    wall: '#243041',
    wallEdge: '#2B3645',
    accent: '#4C8DFF',
    ok: '#4BAE7C',
    warn: '#D7A64B',
    text: '#E8EDF4',
    muted: '#A2ADBD',
    player: '#4C8DFF',
    shadow: 'rgba(0,0,0,0.35)',
  };

  let world = data && data.world ? data.world : null;
  let completed = new Set((data && data.completed) || []);
  let best = Object.assign({}, (data && data.best_scores) || {});
  let locked = !!(data && data.locked);
  let spawn = (data && data.spawn) || (world && world.spawn) || {x: 2, y: 2};

  const keys = Object.create(null);
  const holdDirs = Object.create(null);
  let player = {
    x: Number(spawn.x) || 2,
    y: Number(spawn.y) || 2,
    facing: 'down',
    anim: 0,
  };
  let nearby = null;
  let pathTarget = null;
  let lastInteractAt = 0;
  let running = true;
  let lastTs = 0;
  let dpr = Math.max(1, window.devicePixelRatio || 1);

  function tileAt(tx, ty) {
    if (!world) return '#';
    if (tx < 0 || ty < 0 || tx >= world.width || ty >= world.height) return '#';
    return world.tiles[ty * world.width + tx];
  }

  function blocked(tx, ty) {
    const ch = tileAt(tx, ty);
    return '# C B 1 2 3 4 5 6 D'.split(' ').includes(ch) || ch === ' ';
  }

  function canOccupy(px, py) {
    const r = 0.28;
    const points = [
      [px - r, py - r], [px + r, py - r],
      [px - r, py + r], [px + r, py + r],
    ];
    return points.every(([x, y]) => !blocked(Math.floor(x), Math.floor(y)));
  }

  function resize() {
    if (!world) return;
    const cssW = root.clientWidth || 1000;
    const cssH = Math.min(560, Math.max(360, Math.round(cssW * world.height / world.width * 0.92)));
    canvas.style.width = cssW + 'px';
    canvas.style.height = cssH + 'px';
    canvas.width = Math.floor(cssW * dpr);
    canvas.height = Math.floor(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function findNearby() {
    nearby = null;
    if (!world) return;
    let bestDist = 1.15;
    for (const t of world.terminals) {
      const dx = t.x - player.x;
      const dy = t.y - player.y;
      const dist = Math.hypot(dx, dy);
      if (dist < bestDist) {
        bestDist = dist;
        nearby = t;
      }
    }
    interactBtn.disabled = !nearby || locked;
    if (locked) {
      statusEl.textContent = 'Миссия активна — вернитесь к панели решения ниже.';
    } else if (nearby) {
      const done = completed.has(nearby.mission_id);
      const score = best[nearby.mission_id];
      statusEl.textContent = nearby.label + (done ? ` · пройдена${score != null ? ' (' + score + '%)' : ''}` : ' · нажмите E');
    } else {
      const room = (world.rooms || []).find(r =>
        player.x >= r.x && player.x < r.x + r.w &&
        player.y >= r.y && player.y < r.y + r.h
      );
      statusEl.textContent = room ? room.label : world.title;
    }
  }

  function tryInteract() {
    if (locked || !nearby) return;
    const now = performance.now();
    if (now - lastInteractAt < 500) return;
    lastInteractAt = now;
    setTriggerValue('interact', {
      mission_id: nearby.mission_id,
      terminal: nearby.code,
      x: player.x,
      y: player.y,
      t: Date.now(),
    });
  }

  function move(dx, dy, dt) {
    if (!world || locked) return;
    const speed = world.player_speed || 2.4;
    const dist = speed * dt;
    if (dx || dy) {
      const len = Math.hypot(dx, dy) || 1;
      dx /= len; dy /= len;
      if (Math.abs(dx) > Math.abs(dy)) player.facing = dx > 0 ? 'right' : 'left';
      else player.facing = dy > 0 ? 'down' : 'up';
      player.anim += dt * 8;
      const nx = player.x + dx * dist;
      const ny = player.y + dy * dist;
      if (canOccupy(nx, player.y)) player.x = nx;
      if (canOccupy(player.x, ny)) player.y = ny;
    }
  }

  function followPath(dt) {
    if (!pathTarget) return false;
    const dx = pathTarget.x - player.x;
    const dy = pathTarget.y - player.y;
    if (Math.hypot(dx, dy) < 0.12) {
      pathTarget = null;
      return false;
    }
    move(dx, dy, dt);
    return true;
  }

  function drawRounded(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function draw() {
    if (!world) return;
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    const scale = Math.min(cssW / (world.width * world.tile_size), cssH / (world.height * world.tile_size));
    const tw = world.tile_size * scale;
    const mapW = world.width * tw;
    const mapH = world.height * tw;
    const ox = (cssW - mapW) / 2;
    const oy = (cssH - mapH) / 2;

    ctx.clearRect(0, 0, cssW, cssH);
    ctx.fillStyle = COLORS.bg;
    ctx.fillRect(0, 0, cssW, cssH);

    for (let y = 0; y < world.height; y++) {
      for (let x = 0; x < world.width; x++) {
        const ch = tileAt(x, y);
        const px = ox + x * tw;
        const py = oy + y * tw;
        if (ch === ' ' || ch === undefined) continue;
        if (ch === '#') {
          ctx.fillStyle = COLORS.wall;
          ctx.fillRect(px, py, tw + 0.5, tw + 0.5);
          ctx.fillStyle = COLORS.wallEdge;
          ctx.fillRect(px, py, tw + 0.5, Math.max(2, tw * 0.12));
        } else {
          ctx.fillStyle = ((x + y) % 2 === 0) ? COLORS.floor : COLORS.floorAlt;
          ctx.fillRect(px, py, tw + 0.5, tw + 0.5);
        }
        if (ch === 'C') {
          ctx.fillStyle = '#2a3648';
          drawRounded(px + tw * 0.22, py + tw * 0.18, tw * 0.56, tw * 0.64, 4);
          ctx.fill();
        }
        if (ch === 'B') {
          ctx.fillStyle = '#1d2a3b';
          drawRounded(px + tw * 0.08, py + tw * 0.2, tw * 0.84, tw * 0.55, 3);
          ctx.fill();
          ctx.strokeStyle = COLORS.accent;
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.fillStyle = COLORS.muted;
          ctx.font = `${Math.max(8, tw * 0.22)}px Segoe UI, Arial`;
          ctx.textAlign = 'center';
          ctx.fillText('Прогресс', px + tw / 2, py + tw * 0.52);
        }
      }
    }

    for (const t of world.terminals) {
      const px = ox + (t.x - 0.5) * tw;
      const py = oy + (t.y - 0.5) * tw;
      const done = completed.has(t.mission_id);
      ctx.fillStyle = COLORS.shadow;
      ctx.fillRect(px + tw * 0.18, py + tw * 0.72, tw * 0.64, tw * 0.12);
      ctx.fillStyle = '#152033';
      drawRounded(px + tw * 0.18, py + tw * 0.12, tw * 0.64, tw * 0.62, 4);
      ctx.fill();
      ctx.fillStyle = done ? COLORS.ok : COLORS.accent;
      drawRounded(px + tw * 0.26, py + tw * 0.2, tw * 0.48, tw * 0.32, 3);
      ctx.fill();
      ctx.fillStyle = '#0d1420';
      ctx.fillRect(px + tw * 0.3, py + tw * 0.24, tw * 0.4, tw * 0.24);
      if (nearby && nearby.code === t.code && !locked) {
        ctx.strokeStyle = COLORS.warn;
        ctx.lineWidth = 2;
        drawRounded(px + tw * 0.12, py + tw * 0.06, tw * 0.76, tw * 0.78, 5);
        ctx.stroke();
      }
      ctx.fillStyle = COLORS.text;
      ctx.font = `600 ${Math.max(9, tw * 0.24)}px Segoe UI, Arial`;
      ctx.textAlign = 'center';
      ctx.fillText(t.code, px + tw / 2, py + tw * 0.42);
      ctx.fillStyle = COLORS.muted;
      ctx.font = `${Math.max(8, tw * 0.18)}px Segoe UI, Arial`;
      ctx.fillText(t.short, px + tw / 2, py - tw * 0.08);
      if (done) {
        ctx.fillStyle = COLORS.ok;
        ctx.fillText('✓', px + tw / 2, py + tw * 0.95);
      }
    }

    // Player
    const ppx = ox + player.x * tw;
    const ppy = oy + player.y * tw;
    const bob = Math.sin(player.anim) * tw * 0.03;
    ctx.fillStyle = COLORS.shadow;
    ctx.beginPath();
    ctx.ellipse(ppx, ppy + tw * 0.22, tw * 0.22, tw * 0.1, 0, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = COLORS.player;
    drawRounded(ppx - tw * 0.18, ppy - tw * 0.34 + bob, tw * 0.36, tw * 0.42, 5);
    ctx.fill();
    ctx.fillStyle = '#d9e6ff';
    ctx.beginPath();
    ctx.arc(ppx, ppy - tw * 0.42 + bob, tw * 0.13, 0, Math.PI * 2);
    ctx.fill();
    // facing indicator
    ctx.fillStyle = '#0d1420';
    const eyeY = ppy - tw * 0.44 + bob;
    if (player.facing === 'left') {
      ctx.fillRect(ppx - tw * 0.1, eyeY, tw * 0.05, tw * 0.05);
    } else if (player.facing === 'right') {
      ctx.fillRect(ppx + tw * 0.05, eyeY, tw * 0.05, tw * 0.05);
    } else if (player.facing === 'up') {
      ctx.fillRect(ppx - tw * 0.04, eyeY - tw * 0.02, tw * 0.08, tw * 0.04);
    } else {
      ctx.fillRect(ppx - tw * 0.04, eyeY + tw * 0.02, tw * 0.08, tw * 0.04);
    }

    if (locked) {
      ctx.fillStyle = 'rgba(16,21,29,0.35)';
      ctx.fillRect(0, 0, cssW, cssH);
    }
  }

  function readInput() {
    let dx = 0, dy = 0;
    if (keys['ArrowLeft'] || keys['a'] || keys['A'] || keys['ф'] || keys['Ф'] || holdDirs.left) dx -= 1;
    if (keys['ArrowRight'] || keys['d'] || keys['D'] || keys['в'] || keys['В'] || holdDirs.right) dx += 1;
    if (keys['ArrowUp'] || keys['w'] || keys['W'] || keys['ц'] || keys['Ц'] || holdDirs.up) dy -= 1;
    if (keys['ArrowDown'] || keys['s'] || keys['S'] || keys['ы'] || keys['Ы'] || holdDirs.down) dy += 1;
    return [dx, dy];
  }

  function frame(ts) {
    if (!running) return;
    const dt = Math.min(0.05, (ts - lastTs) / 1000 || 0.016);
    lastTs = ts;
    if (!followPath(dt)) {
      const [dx, dy] = readInput();
      if (dx || dy) pathTarget = null;
      move(dx, dy, dt);
    }
    findNearby();
    draw();
    requestAnimationFrame(frame);
  }

  function onKeyDown(e) {
    keys[e.key] = true;
    if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight',' '].includes(e.key) ||
        ['w','a','s','d','W','A','S','D','ц','Ц','ф','Ф','ы','Ы','в','В','e','E','у','У'].includes(e.key)) {
      e.preventDefault();
    }
    if (e.key === 'e' || e.key === 'E' || e.key === 'у' || e.key === 'У') tryInteract();
  }
  function onKeyUp(e) { keys[e.key] = false; }

  canvas.addEventListener('keydown', onKeyDown);
  canvas.addEventListener('keyup', onKeyUp);
  canvas.addEventListener('click', (e) => {
    if (!world || locked) return;
    canvas.focus();
    const rect = canvas.getBoundingClientRect();
    const cssW = canvas.clientWidth;
    const cssH = canvas.clientHeight;
    const scale = Math.min(cssW / (world.width * world.tile_size), cssH / (world.height * world.tile_size));
    const tw = world.tile_size * scale;
    const ox = (cssW - world.width * tw) / 2;
    const oy = (cssH - world.height * tw) / 2;
    const x = (e.clientX - rect.left - ox) / tw;
    const y = (e.clientY - rect.top - oy) / tw;
    if (x >= 0 && y >= 0 && x < world.width && y < world.height && !blocked(Math.floor(x), Math.floor(y))) {
      pathTarget = { x, y };
    }
  });

  root.querySelectorAll('button[data-dir]').forEach((btn) => {
    const dir = btn.getAttribute('data-dir');
    const start = (ev) => { ev.preventDefault(); holdDirs[dir] = true; canvas.focus(); };
    const stop = (ev) => { ev.preventDefault(); holdDirs[dir] = false; };
    btn.addEventListener('mousedown', start);
    btn.addEventListener('mouseup', stop);
    btn.addEventListener('mouseleave', stop);
    btn.addEventListener('touchstart', start, {passive: false});
    btn.addEventListener('touchend', stop);
    btn.addEventListener('touchcancel', stop);
  });
  interactBtn.addEventListener('click', () => { canvas.focus(); tryInteract(); });

  const onResize = () => { dpr = Math.max(1, window.devicePixelRatio || 1); resize(); draw(); };
  window.addEventListener('resize', onResize);

  if (!world) {
    statusEl.textContent = 'Карта не загружена.';
  } else {
    resize();
    canvas.focus();
    requestAnimationFrame(frame);
  }

  return () => {
    running = false;
    window.removeEventListener('resize', onResize);
    canvas.removeEventListener('keydown', onKeyDown);
    canvas.removeEventListener('keyup', onKeyUp);
  };
}
"""

_world_component = st_components.component(
    "cyberquest_world",
    html=_WORLD_HTML,
    css=_WORLD_CSS,
    js=_WORLD_JS,
    isolate_styles=True,
)


def world_payload(
    world: WorldMap,
    *,
    completed: list[str] | None = None,
    best_scores: dict[str, float] | None = None,
    spawn: tuple[float, float] | None = None,
    locked: bool = False,
) -> dict[str, Any]:
    """Serialize world data for the canvas (trusted content only)."""
    sx, sy = spawn or world.spawn
    return {
        "locked": bool(locked),
        "spawn": {"x": float(sx), "y": float(sy)},
        "completed": list(completed or []),
        "best_scores": {k: float(v) for k, v in (best_scores or {}).items()},
        "world": {
            "title": world.title,
            "tile_size": world.tile_size,
            "player_speed": world.player_speed,
            "width": world.width,
            "height": world.height,
            "tiles": "".join(world.tiles),
            "spawn": {"x": world.spawn[0], "y": world.spawn[1]},
            "rooms": list(world.rooms),
            "terminals": [
                {
                    "code": t.code,
                    "mission_id": t.mission_id,
                    "label": t.label,
                    "short": t.short,
                    "x": t.x,
                    "y": t.y,
                }
                for t in world.terminals.values()
            ],
        },
    }


def render_world(
    world: WorldMap,
    *,
    completed: list[str] | None = None,
    best_scores: dict[str, float] | None = None,
    spawn: tuple[float, float] | None = None,
    locked: bool = False,
    key: str = "cq_world",
) -> Any:
    """Mount the 2D world. Returns the component result (may include interact trigger)."""
    return _world_component(
        data=world_payload(
            world,
            completed=completed,
            best_scores=best_scores,
            spawn=spawn,
            locked=locked,
        ),
        key=key,
        on_interact_change=lambda: None,
    )
