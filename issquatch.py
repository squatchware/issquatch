"""issquatch: the International Space Station, tracked from the woods.

A floating TUI for Omarchy: a world map with the day/night line, the ISS and its ground track,
live telemetry, and the next passes you can actually see from where you are. `issquatch notify`
(run by a systemd timer) sends a desktop notification before a good one.

Orbit maths: CelesTrak's TLE for the ISS, propagated with SGP4 (the `sgp4` package), rotated
into Earth-fixed coordinates with GMST. The sun comes from the Astronomical Almanac's
low-precision formulae, which are good to about 0.01 degrees and plenty for a shadow line.
"""
import base64, json, math, os, select, shutil, signal, subprocess, sys, termios, threading, time, tty, zlib
import tomllib, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sgp4.api import Satrec, jday

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "issquatch" / "config.toml"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "issquatch"
THEME = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR=25544&FORMAT=tle"
TLE_MAX_AGE = 12 * 3600
# Crew and docked spacecraft: the community-maintained ISS APIs (github.com/corquaid)
CREW_URL = "https://corquaid.github.io/international-space-station-APIs/JSON/people-in-space.json"
DOCKED_URL = "https://corquaid.github.io/international-space-station-APIs/JSON/iss-docked-spacecraft.json"
CREW_MAX_AGE = 6 * 3600
ISS_LAUNCH = datetime(1998, 11, 20, 6, 40, tzinfo=timezone.utc)  # Zarya, the first module
UA = f"issquatch/{VERSION} (+https://squatchware.dev/issquatch/)"

RE = 6378.137            # WGS84 equatorial radius, km
F = 1 / 298.257223563    # WGS84 flattening
DEG = math.pi / 180


# ============================================================ config and data

def load_config():
    try:
        return tomllib.loads(CONFIG.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def save_config(cfg):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in cfg.items():
        lines.append(f'{k} = {json.dumps(v)}' if isinstance(v, str) else f"{k} = {v}")
    CONFIG.write_text("# issquatch settings. `issquatch setup` rewrites this file.\n" + "\n".join(lines) + "\n")


def http_get(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def load_tle(force=False):
    """Return (line1, line2, fetched_at). Uses the cache while it's fresh, and keeps using a
    stale one if CelesTrak can't be reached."""
    path = CACHE / "iss.tle"
    fresh = path.exists() and time.time() - path.stat().st_mtime < TLE_MAX_AGE
    if force or not fresh:
        try:
            lines = [l.strip() for l in http_get(TLE_URL).splitlines() if l.strip()]
            l1, l2 = next(l for l in lines if l.startswith("1 ")), next(l for l in lines if l.startswith("2 "))
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{l1}\n{l2}\n")
        except Exception:
            if not path.exists():
                raise SystemExit("issquatch: can't reach CelesTrak for the ISS orbit, and there's no cached copy yet.")
    l1, l2 = path.read_text().split("\n")[:2]
    return l1, l2, path.stat().st_mtime


def load_land():
    raw = base64.b64decode((HERE / "share" / "land.bits").read_text())
    return raw, 720, 360


def is_land(land, lat, lon):
    raw, w, h = land
    x = int((lon + 180) / 360 * w) % w
    y = min(h - 1, max(0, int((90 - lat) / 180 * h)))
    i = y * w + x
    return raw[i >> 3] >> (7 - (i & 7)) & 1


_COUNTRIES = None
SHORT_NAMES = {"United States of America": "United States", "Dem. Rep. Congo": "DR Congo",
               "Central African Rep.": "Central African Rep", "Bosnia and Herz.": "Bosnia & Herzegovina"}


def load_countries():
    global _COUNTRIES
    if _COUNTRIES is None:
        try:
            d = json.loads((HERE / "share" / "countries.bin").read_text())
            _COUNTRIES = (zlib.decompress(base64.b64decode(d["grid"])), d["w"], d["h"], d["names"])
        except (OSError, ValueError, zlib.error):
            _COUNTRIES = (b"", 0, 0, [])
    return _COUNTRIES


def sea_name(lat, lon):
    """Good enough for a status line: the oceans, plus the seas people ask about."""
    if lat < -60:
        return "Southern Ocean"
    if lat > 66:
        return "Arctic Ocean"
    if 30 < lat < 46 and -6 < lon < 36:
        return "Mediterranean Sea"
    if 10 < lat < 31 and -98 < lon < -81:
        return "Gulf of Mexico"
    if 9 < lat < 22 and -88 < lon < -60:
        return "Caribbean Sea"
    if 36 < lat < 48 and 47 < lon < 55:
        return "Caspian Sea"
    if 12 < lat < 30 and 32 < lon < 44:
        return "Red Sea"
    if -70 < lon < 20 or (lat > 45 and -80 < lon < -70) or (lat < -40 and -70 < lon < 20):
        return ("North" if lat >= 0 else "South") + " Atlantic"
    if 20 <= lon < 147 and lat < 30 and not (lon > 120 and lat > -5):
        return "Indian Ocean"
    return ("North" if lat >= 0 else "South") + " Pacific"


def over(lat, lon):
    """The country (or sea) under a point."""
    grid, w, h, names = load_countries()
    if w:
        x = int((lon + 180) / 360 * w) % w
        y = min(h - 1, max(0, int((90 - lat) / 180 * h)))
        i = grid[y * w + x]
        if i:
            return SHORT_NAMES.get(names[i - 1], names[i - 1])
    return sea_name(lat, lon)


# ============================================================ crew and docked spacecraft

def load_station_info(force=False):
    """People in space and docked spacecraft, cached for six hours. Returns None when there's
    neither a cache nor a connection."""
    path = CACHE / "station.json"
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        cached = None
    if cached and not force and time.time() - cached.get("fetched", 0) < CREW_MAX_AGE:
        return cached
    try:
        info = {"people": json.loads(http_get(CREW_URL)), "docked": json.loads(http_get(DOCKED_URL)),
                "fetched": time.time()}
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(info))
        return info
    except Exception:
        return cached


def flag(code, style="emoji"):
    """A country's flag as its regional-indicator emoji pair (two cells wide), or its letters."""
    code = (code or "").lower()
    if len(code) != 2 or not code.isalpha():
        return "  "
    if style == "letters":
        return code.upper()
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in code)


def crew(info):
    """(aboard the ISS, everyone else in space, expedition number)."""
    if not info:
        return [], [], None
    people = info["people"].get("people", [])
    aboard = sorted([p for p in people if p.get("iss")],
                    key=lambda p: ("Commander" not in (p.get("position") or ""), p.get("launched", 0), p.get("name", "")))
    return aboard, [p for p in people if not p.get("iss")], info["people"].get("iss_expedition")


def next_light_change(iss, now):
    """When the station next crosses into or out of Earth's shadow (about 16 sunrises a day)."""
    first = iss.at(now)
    if not first:
        return None
    for k in range(1, 400):  # 20 s steps, a little over one orbit
        t = now + timedelta(seconds=20 * k)
        s = iss.at(t)
        if s and s["sunlit"] != first["sunlit"]:
            return ("sunset" if first["sunlit"] else "sunrise"), t
    return None


# ============================================================ time, sun, frames

def jd_of(dt):
    jd, fr = jday(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second + dt.microsecond / 1e6)
    return jd, fr


def gmst(jd, fr):
    """Greenwich mean sidereal time in radians (IAU 1982)."""
    t = ((jd - 2451545.0) + fr) / 36525.0
    s = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return (s % 86400) / 86400 * 2 * math.pi


def sun(jd, fr):
    """Sun's unit vector (equatorial, of date) and its right ascension and declination, radians."""
    n = (jd - 2451545.0) + fr
    L = (280.460 + 0.9856474 * n) % 360
    g = (357.528 + 0.9856003 * n) % 360 * DEG
    lam = (L + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) * DEG
    eps = (23.439 - 0.0000004 * n) * DEG
    x, y, z = math.cos(lam), math.cos(eps) * math.sin(lam), math.sin(eps) * math.sin(lam)
    return (x, y, z), math.atan2(y, x), math.asin(z)


def subsolar(jd, fr):
    _, ra, dec = sun(jd, fr)
    lon = (ra - gmst(jd, fr)) / DEG
    return dec / DEG, (lon + 540) % 360 - 180


def sun_elevation(lat, lon, sub):
    slat, slon = sub[0] * DEG, sub[1] * DEG
    lat, lon = lat * DEG, lon * DEG
    return math.asin(math.sin(lat) * math.sin(slat) + math.cos(lat) * math.cos(slat) * math.cos(lon - slon)) / DEG


def teme_to_ecef(r, theta):
    c, s = math.cos(theta), math.sin(theta)
    return (c * r[0] + s * r[1], -s * r[0] + c * r[1], r[2])


def ecef_to_geodetic(x, y, z):
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    e2 = F * (2 - F)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(5):
        n = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        alt = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + alt)))
    n = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    alt = p / math.cos(lat) - n
    return lat / DEG, lon / DEG, alt


def geodetic_to_ecef(lat, lon, alt=0.0):
    lat, lon = lat * DEG, lon * DEG
    e2 = F * (2 - F)
    n = RE / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + alt) * math.cos(lat) * math.cos(lon), (n + alt) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + alt) * math.sin(lat))


def look_angles(obs_lat, obs_lon, sat_ecef):
    """Azimuth and elevation of a satellite from an observer, degrees."""
    ox, oy, oz = geodetic_to_ecef(obs_lat, obs_lon)
    dx, dy, dz = sat_ecef[0] - ox, sat_ecef[1] - oy, sat_ecef[2] - oz
    lat, lon = obs_lat * DEG, obs_lon * DEG
    s = math.sin(lat) * math.cos(lon) * dx + math.sin(lat) * math.sin(lon) * dy - math.cos(lat) * dz
    e = -math.sin(lon) * dx + math.cos(lon) * dy
    z = math.cos(lat) * math.cos(lon) * dx + math.cos(lat) * math.sin(lon) * dy + math.sin(lat) * dz
    el = math.atan2(z, math.hypot(s, e)) / DEG
    az = (math.atan2(e, -s) / DEG) % 360
    return az, el


def in_shadow(r, sun_vec):
    """Cylindrical Earth-shadow test: behind the Earth and inside its radius."""
    d = r[0] * sun_vec[0] + r[1] * sun_vec[1] + r[2] * sun_vec[2]
    if d > 0:
        return False
    perp = math.sqrt(max(0.0, r[0] ** 2 + r[1] ** 2 + r[2] ** 2 - d * d))
    return perp < RE


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(az):
    return COMPASS[round(az / 22.5) % 16]


# ============================================================ the ISS

class ISS:
    def __init__(self, force=False):
        self.l1, self.l2, self.fetched = load_tle(force)
        self.sat = Satrec.twoline2rv(self.l1, self.l2)

    @property
    def epoch(self):
        return datetime(2000 + self.sat.epochyr if self.sat.epochyr < 57 else 1900 + self.sat.epochyr, 1, 1,
                        tzinfo=timezone.utc) + timedelta(days=self.sat.epochdays - 1)

    def at(self, dt):
        """State at a UTC datetime: TEME position, ECEF position, lat/lon/alt, speed, sunlit."""
        jd, fr = jd_of(dt)
        err, r, v = self.sat.sgp4(jd, fr)
        if err:
            return None
        theta = gmst(jd, fr)
        ecef = teme_to_ecef(r, theta)
        lat, lon, alt = ecef_to_geodetic(*ecef)
        sun_vec, _, _ = sun(jd, fr)
        return {
            "time": dt, "teme": r, "ecef": ecef, "lat": lat, "lon": lon, "alt": alt,
            "speed": math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) * 3600,
            "sunlit": not in_shadow(r, sun_vec), "subsolar": subsolar(jd, fr),
        }

    def orbit_number(self, dt):
        rev = int(self.l2[63:68])
        return rev + int((dt - self.epoch).total_seconds() / 86400 * self.sat.no_kozai * 1440 / (2 * math.pi))

    def elevation(self, dt, lat, lon):
        s = self.at(dt)
        return look_angles(lat, lon, s["ecef"])[1] if s else -90

    def passes(self, lat, lon, start=None, days=3, min_el=10.0):
        """Passes above `min_el`, each with its visible stretch: ISS sunlit while the sky is dark
        (sun below -6 degrees) at the observer."""
        start = start or datetime.now(timezone.utc)
        end = start + timedelta(days=days)
        step = timedelta(seconds=30)
        out, t, prev = [], start, self.elevation(start, lat, lon)
        rise = start if prev >= min_el else None

        def edge(a, b, rising):
            for _ in range(12):  # bisect to about a second
                m = a + (b - a) / 2
                if (self.elevation(m, lat, lon) >= min_el) == rising:
                    b = m
                else:
                    a = m
            return b

        while t < end:
            n = t + step
            el = self.elevation(n, lat, lon)
            if prev < min_el <= el:
                rise = edge(t, n, True)
            elif prev >= min_el > el and rise:
                out.append(self._describe(rise, edge(t, n, False), lat, lon))
                rise = None
            prev, t = el, n
        return out

    def _describe(self, rise, set_, lat, lon):
        best, best_el, vis = rise, -90, []
        t = rise
        while t <= set_:
            s = self.at(t)
            az, el = look_angles(lat, lon, s["ecef"])
            if el > best_el:
                best, best_el = t, el
            if s["sunlit"] and sun_elevation(lat, lon, s["subsolar"]) < -6:
                vis.append(t)
            t += timedelta(seconds=10)
        for dt in range(-10, 11):  # the peak to the second, which matters on near-overhead passes
            m = best + timedelta(seconds=dt)
            el = self.elevation(m, lat, lon)
            if el > best_el:
                best, best_el = m, el
        az0 =look_angles(lat, lon, self.at(vis[0] if vis else rise)["ecef"])[0]
        az1 = look_angles(lat, lon, self.at(vis[-1] if vis else set_)["ecef"])[0]
        return {
            "rise": rise, "set": set_, "max_time": best, "max_el": best_el,
            "visible": bool(vis), "vis_start": vis[0] if vis else None, "vis_end": vis[-1] if vis else None,
            "from": compass(az0), "to": compass(az1),
        }


def visible_passes(iss, cfg, days=3):
    if "lat" not in cfg:
        return None
    return [p for p in iss.passes(cfg["lat"], cfg["lon"], days=days, min_el=cfg.get("min_elevation", 10))
            if p["visible"]]


# ============================================================ colours

FALLBACK = {  # Squatchware, for when there's no Omarchy theme to wear
    "background": "#0F1A14", "foreground": "#F4E4BC", "accent": "#D4A832", "muted": "#AB9E7B",
    "blue": "#5B8FB9", "green": "#6FAE5A", "yellow": "#D4A832", "red": "#DB6250", "cyan": "#3AA597",
    "orange": "#D4722A", "bright_foreground": "#FFF8DC", "lighter_background": "#183826",
}


def load_theme():
    pal = dict(FALLBACK)
    try:
        pal.update({k: v for k, v in tomllib.loads(THEME.read_text()).items() if isinstance(v, str)})
    except (OSError, tomllib.TOMLDecodeError):
        pass
    return {k: hexrgb(v) for k, v in pal.items() if v.startswith("#") and len(v) == 7}


def hexrgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def fg(c):
    return f"\x1b[38;2;{c[0]};{c[1]};{c[2]}m"


def bg(c):
    return f"\x1b[48;2;{c[0]};{c[1]};{c[2]}m"


RESET = "\x1b[0m"
BOLD = "\x1b[1m"


# ============================================================ drawing

# The squatch in his space helmet, 32x32 (Squatchware brand art: not covered by the MIT licence,
# not for reuse). "~" is the helmet glass, tinted from the theme at draw time.
HEAD = """\
..............hhhh..............
..........hhhhhhhhhhhh..........
........hhh~~~~~~~~~~hhh........
.......hh~H~~~~~~~~~~~~hh.......
......hh~HH~~~~~~~~~~~~~hh......
.....hhHHH~~~~~~~~~~~~~H~hh.....
....hh~HH~~~RRRRRRRR~~~~H~hh....
....h~HH~~RRRRRRRRRRRR~~~~~h....
...hhHH~~RRRRRRGGRRRRRR~~~~hh...
...h~H~~RRRRRRRGGRRRRRRR~~~~h...
..hh~H~bbbbbbbbbbbbbbbbbb~~~hh..
..h~~~~oFFLFFFFFFFFFFLFFo~~~~h..
..h~~~oFFFFFFFFFFFFFFFFFFo~~~h..
..h~~~oFDDDDDDFFFFDDDDDDFo~~~h..
..h~~~oFLPPPPPFFFFPPPPPLFo~~~h..
..h~~~oFLPPeWPFFFFPeWPPLFo~~~h..
..h~~~oFFPPeePFDDFPeePPFFo~~~h..
..h~~~oFLFPPPPDDDDPPPPFLFo~~~h..
..hh~~oFFFPPPDDDDDDPPPFFFo~~hh..
...h~~oFLFFPPPoooooPPPFLFo~~h...
...hh~oFFFFPPPPPPPPPPFFFFo~hh...
....h~~oFFLFFPPPPPPFFLFFo~~h....
....hh~~oFFFFFFFFFFFFFFo~~hh....
.....hh~~ooFFFFFFFFFFoo~~hh.....
......hh~~~oooooooooo~~~hh......
.......hh~~~~~~~~~~~~~~hh.......
....kkkkkkkkkkkkkkkkkkkkkkk.....
...krrrrrrrrrrrrrrrrrrrrrrrk....
..krrrrrrrrrrOGGGGOrrrrrrrrrk...
..krrrrrrrrrrOGGGGOrrrrrrrrrk...
..krrrrrrrrrrrrrrrrrrrrrrrrrk...
...kkkkkkkkkkkkkkkkkkkkkkkkk....""".split("\n")
HEAD_PAL = {"R": "#a83232", "b": "#6e1f1f", "G": "#d4a832", "O": "#b8862a", "o": "#1a1a2e", "e": "#1a1a2e",
            "W": "#fff8dc", "F": "#a0582a", "L": "#d5a269", "D": "#542e27", "P": "#e9d19f", "h": "#cfe3f2",
            "H": "#ffffff", "r": "#c8ccd4", "k": "#6b7280"}

# The station: solar wings either side of the truss, modules down the middle.
STATION = {
    "fine": ["###.#...#.###", "###.#...#.###", "###.#.#.#.###", "#############",
             "###.#.#.#.###", "###.#...#.###", "###.#...#.###"],
    "coarse": ["#.#.#", "#####", "#.#.#"],
}

# Octants: 2x4 pixels per character cell. Unicode 16 has 230 of them; the other 26 patterns are
# older block characters. Bit n is pixel n+1, counted left to right, top to bottom.
OCTANTS = {}
for _cp in range(0x1CD00, 0x1CDE6):
    _n = __import__("unicodedata").name(chr(_cp), "")
    if _n.startswith("BLOCK OCTANT-"):
        OCTANTS[sum(1 << (int(d) - 1) for d in _n.split("-")[1])] = chr(_cp)
if len(OCTANTS) < 230:  # a Python without Unicode 16 data: the code points are fixed, so rebuild them
    _missing = {0, 1, 2, 3, 5, 10, 15, 20, 40, 63, 64, 80, 85, 90, 95, 128, 160, 165, 170, 175, 192, 240, 245, 250, 252, 255}
    OCTANTS = dict(zip([m for m in range(256) if m not in _missing], map(chr, range(0x1CD00, 0x1CDE6))))
OCTANTS.update({0: " ", 1: "\U0001CEA8", 2: "\U0001CEAB", 3: "\U0001FB82", 5: "▘", 10: "▝", 15: "▀", 20: "\U0001FBE6",
                40: "\U0001FBE7", 63: "\U0001FB85", 64: "\U0001CEA3", 80: "▖", 85: "▌", 90: "▞", 95: "▛", 128: "\U0001CEA0",
                160: "▗", 165: "▚", 170: "▐", 175: "▜", 192: "▂", 240: "▄", 245: "▙", 250: "▟", 252: "▆", 255: "█"})
HALVES = {0: " ", 1: "▀", 2: "▄", 3: "█"}


def block_mode(cfg):
    """Octants where the terminal draws them itself (Ghostty, kitty); half blocks everywhere else."""
    want = os.environ.get("ISSQUATCH_BLOCKS") or cfg.get("blocks", "auto")
    if want in ("octant", "half"):
        return want
    term = (os.environ.get("TERM_PROGRAM", "") + " " + os.environ.get("TERM", "")).lower()
    return "octant" if ("ghostty" in term or "kitty" in term) else "half"


class Screen:
    """Pixels drawn sx by sy to a character cell (2x4 octants or 1x2 half blocks), plus a text layer.
    Each cell can only show two colours, so a cell with more keeps the pair that fits it best."""

    def __init__(self, cols, rows, mode):
        self.cols, self.rows, self.mode = cols, rows, mode
        self.sx, self.sy = (2, 4) if mode == "octant" else (1, 2)
        self.w, self.h = cols * self.sx, rows * self.sy
        self.px = [[None] * self.w for _ in range(self.h)]
        self.text = {}

    def put(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            self.px[y][x] = c

    def write(self, x, y, s, colour, bold=False):
        """Text at a cell. A flag (two regional indicators) is one glyph two cells wide: it's
        stored in the first cell and the second is left empty so the columns stay lined up."""
        i = 0
        while i < len(s):
            ch = s[i]
            if 0x1F1E6 <= ord(ch) <= 0x1F1FF and i + 1 < len(s) and 0x1F1E6 <= ord(s[i + 1]) <= 0x1F1FF:
                if 0 <= x < self.cols - 1 and 0 <= y < self.rows:
                    self.text[(x, y)] = (ch + s[i + 1], colour, bold)
                    self.text[(x + 1, y)] = ("", colour, bold)
                x, i = x + 2, i + 2
                continue
            if 0 <= x < self.cols and 0 <= y < self.rows:
                self.text[(x, y)] = (ch, colour, bold)
            x, i = x + 1, i + 1

    def cell(self, x, y, base):
        sx, sy = self.sx, self.sy
        return [self.px[y * sy + j][x * sx + i] or base for j in range(sy) for i in range(sx)]

    @staticmethod
    def two_colours(pix):
        counts = {}
        for p in pix:
            counts[p] = counts.get(p, 0) + 1
        if len(counts) <= 2:
            cs = sorted(counts, key=counts.get, reverse=True)
            return cs[0], cs[-1]
        dist = lambda a, b: (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
        best, pair = None, None
        cs = list(counts)
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                err = sum(n * min(dist(c, cs[i]), dist(c, cs[j])) for c, n in counts.items())
                if best is None or err < best:
                    best, pair = err, (cs[i], cs[j])
        a, b = pair
        return (a, b) if counts[a] >= counts[b] else (b, a)

    def render(self, base):
        glyphs = OCTANTS if self.mode == "octant" else HALVES
        dist = lambda a, b: (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2
        out = []
        for row in range(self.rows):
            line, cur_fg, cur_bg = [f"\x1b[{row + 1};1H"], None, None
            for x in range(self.cols):
                pix = self.cell(x, row, base)
                back, front = self.two_colours(pix)
                if (x, row) in self.text:
                    ch, c, b = self.text[(x, row)]
                    if ch:  # empty: the right half of a wide glyph drawn in the cell before
                        line.append(bg(back) + fg(c) + (BOLD if b else "") + ch + RESET)
                    cur_fg = cur_bg = None
                    continue
                mask = 0
                if front != back:
                    for i, p in enumerate(pix):
                        if dist(p, front) < dist(p, back):
                            mask |= 1 << i
                seq = ""
                if back != cur_bg:
                    seq += bg(back)
                    cur_bg = back
                if mask and front != cur_fg:
                    seq += fg(front)
                    cur_fg = front
                line.append(seq + glyphs[mask])
            out.append("".join(line) + RESET)
        return "".join(out)


_MAP_CACHE = {}


def map_layer(land, w, h, pal, sub):
    """Land, sea and night for a w x h pixel map. The sun moves a quarter of a degree a minute,
    so the shading is recomputed at most once a minute; the coastline once per size."""
    key = ("land", w, h)
    if key not in _MAP_CACHE:
        _MAP_CACHE.clear()
        _MAP_CACHE[key] = [[is_land(land, 90 - (y + .5) / h * 180, (x + .5) / w * 360 - 180) for x in range(w)]
                           for y in range(h)]
    skey = ("shade", w, h, tuple(sorted(pal.items())), round(sub[0], 1), round(sub[1] / .25))
    if skey in _MAP_CACHE:
        return _MAP_CACHE[skey]
    for k in [k for k in _MAP_CACHE if k[0] == "shade"]:
        del _MAP_CACHE[k]
    mask = _MAP_CACHE[key]
    ocean = mix(pal["background"], pal["blue"], .28)
    ground = mix(pal["background"], pal["green"], .55)
    # night pulls towards the dark end of the theme, whichever end of the palette that is
    light = sum(pal["background"]) > sum(pal["foreground"])
    dark = mix(pal["foreground"], pal["background"], .35) if light else mix(pal["background"], (0, 0, 0), .5)
    slat, slon = sub[0] * DEG, sub[1] * DEG
    ss, cs = math.sin(slat), math.cos(slat)
    coslon = [math.cos((x + .5) / w * 2 * math.pi - math.pi - slon) for x in range(w)]
    shades = {}
    rows = []
    for y in range(h):
        lat = (90 - (y + .5) / h * 180) * DEG
        a, b = math.sin(lat) * ss, math.cos(lat) * cs
        row = []
        for x in range(w):
            c = ground if mask[y][x] else ocean
            s = a + b * coslon[x]
            if s < 0:  # night, with a soft civil-twilight edge
                t = min(.6, .3 + math.asin(max(-1.0, s)) / DEG / -18 * .3)
                k = (c, round(t, 2))
                if k not in shades:
                    shades[k] = mix(c, dark, k[1])
                c = shades[k]
            row.append(c)
        rows.append(row)
    _MAP_CACHE[skey] = rows
    return rows


def draw_map(scr, top, w_cells, h_cells, land, iss, now_state, cfg, pal, show_track):
    """Equirectangular map filling w_cells x h_cells character cells from row `top`."""
    w, h = w_cells * scr.sx, h_cells * scr.sy
    y0 = top * scr.sy
    layer = map_layer(land, w, h, pal, now_state["subsolar"])
    for y in range(h):
        scr.px[y0 + y][:w] = layer[y]

    def to_px(lat, lon):
        return int((lon + 180) / 360 * w) % w, y0 + min(h - 1, int((90 - lat) / 180 * h))

    fine = scr.mode == "octant"
    if show_track:
        now = now_state["time"]
        past = mix(pal["muted"], pal["background"], .35)
        for k in range(-135, 277):  # every 20 s, from 45 minutes ago to 92 minutes ahead
            s = iss.at(now + timedelta(seconds=20 * k))
            if not s or k == 0:
                continue
            x, y = to_px(s["lat"], s["lon"])
            if k < 0:
                scr.put(x, y, past)
            elif (k // (3 if fine else 6)) % 2 == 0:  # dashed ahead
                scr.put(x, y, pal["accent"])

    sx, sy = to_px(*now_state["subsolar"])
    for dx, dy in (((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)) if fine else ((0, 0),)):
        scr.put(sx + dx, sy + dy, pal["yellow"])
    if "lat" in cfg:
        ox, oy = to_px(cfg["lat"], cfg["lon"])
        arm = 2 if fine else 1
        for d in range(-arm, arm + 1):
            scr.put(ox + d, oy, pal["red"])
            scr.put(ox, oy + d, pal["red"])
        scr.put(ox, oy, pal["bright_foreground"])
    ix, iy = to_px(now_state["lat"], now_state["lon"])
    sprite = STATION["fine" if fine else "coarse"]
    cx, cy = len(sprite[0]) // 2, len(sprite) // 2
    for dy, row in enumerate(sprite):
        for dx, ch in enumerate(row):
            if ch == "#":
                scr.put((ix - cx + dx) % w, iy - cy + dy, pal["bright_foreground"])


def draw_head(scr, x, y, pal):
    """The helmeted squatch with its top-left at cell (x, y): 16x8 cells in octants, and in half
    blocks every other pixel of it, which keeps the same footprint."""
    glass = mix(pal["background"], (120, 190, 235), .22)
    step = 1 if scr.mode == "octant" else 2
    for py in range(0, 32, step):
        for px in range(0, 32, step):
            ch = HEAD[py][px]
            if ch == ".":
                continue
            c = glass if ch == "~" else hexrgb(HEAD_PAL[ch])
            scr.put(x * scr.sx + px // step, y * scr.sy + py // step, c)


def fmt_dur(sec):
    sec = int(sec)
    if sec < 0:
        return "now"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s"


def local(dt):
    return dt.astimezone()


def live_lines(iss, st, pal, light):
    """The seven lines beside the squatch."""
    L = []
    add = lambda s="", c="foreground", b=False: L.append((s, pal[c], b))
    ns = "N" if st["lat"] >= 0 else "S"
    ew = "E" if st["lon"] >= 0 else "W"
    add("LIVE", "cyan", True)
    add(f"Lat   {abs(st['lat']):6.2f}° {ns}")
    add(f"Lon   {abs(st['lon']):6.2f}° {ew}")
    add(f"Alt   {st['alt']:6.0f} km")
    add(f"Speed {st['speed']:6,.0f} km/h")
    add("Over " + over(st["lat"], st["lon"]), "accent")
    if light:
        kind, t = light
        mins = round((t - st["time"]).total_seconds() / 60)  # found in 20 s steps, so minutes are honest
        add(f"{'Sunset' if kind == 'sunset' else 'Sunrise'} in {mins} min" if mins >= 1 else
            f"{'Sunset' if kind == 'sunset' else 'Sunrise'} now",
            "yellow" if st["sunlit"] else "muted")
    else:
        add("In sunlight" if st["sunlit"] else "In Earth's shadow", "yellow" if st["sunlit"] else "muted")
    return L


def passes_lines(st, cfg, passes, pal):
    L = []
    add = lambda s="", c="foreground", b=False: L.append((s, pal[c], b))
    if "lat" not in cfg:
        add("Set your location:", "muted")
        add("issquatch setup", "accent")
        return L
    place = cfg.get("place") or f"{cfg['lat']:.2f}, {cfg['lon']:.2f}"
    add("Visible from " + place, "muted")
    now = st["time"]
    az, el = look_angles(cfg["lat"], cfg["lon"], st["ecef"])
    if el > 0:
        add(f"Above your horizon: {el:.0f}° {compass(az)}", "accent", True)
    if passes is None:
        add("working it out…", "muted")
    elif not passes:
        add("None in the next 3 days.", "muted")
        add("Lit passes need a dark sky", "muted")
        add("and a sunlit ISS.", "muted")
    else:
        p = passes[0]
        t0 = p["vis_start"]
        add()
        add(local(t0).strftime("%a %d %b  %H:%M"), "accent", True)
        add(f"in {fmt_dur((t0 - now).total_seconds())}", "accent")
        add(f"Up to {p['max_el']:.0f}°, {p['from']} → {p['to']}")
        add(f"{fmt_dur((p['vis_end'] - t0).total_seconds())} visible", "muted")
        if len(passes) > 1:
            add()
            add("LATER", "cyan", True)
            for q in passes[1:6]:
                add(f"{local(q['vis_start']).strftime('%a %H:%M')}  {q['max_el']:3.0f}°  {q['from']}→{q['to']}",
                    "foreground" if q["max_el"] >= 40 else "muted")
    return L


def crew_lines(info, pal, flags):
    L = []
    add = lambda s="", c="foreground", b=False: L.append((s, pal[c], b))
    aboard, elsewhere, expedition = crew(info)
    if not info:
        add("Crew list not loaded yet.", "muted")
        add("It needs a connection once.", "muted")
        return L
    add(f"Expedition {expedition} · {len(aboard)} aboard" if expedition else f"{len(aboard)} aboard", "accent", True)
    now_ts = time.time()
    for person in aboard:
        name = person.get("name", "?")
        if "Commander" in (person.get("position") or ""):
            name += " ★"
        days = int((now_ts - person.get("launched", now_ts)) // 86400)
        add(f"{flag(person.get('flag_code'), flags)} {name[:18]:<18} {(person.get('agency') or '')[:9]:<9} {days:>3}d")
    if elsewhere:
        add()
        add("ALSO IN SPACE", "cyan", True)
        groups = {}
        for person in elsewhere:
            groups.setdefault((person.get("spacecraft") or "elsewhere", person.get("flag_code")), []).append(person)
        for (craft, code), group in groups.items():
            where = f"on Tiangong ({craft})" if "Shenzhou" in craft else f"aboard {craft}"
            add(f"{flag(code, flags)} {len(group)} {where}", "muted")
    add()
    add(f"{len(aboard) + len(elsewhere)} people in space right now", "foreground", True)
    return L


def station_lines(iss, st, info, pal, flags):
    L = []
    add = lambda s="", c="foreground", b=False: L.append((s, pal[c], b))
    docked = (info or {}).get("docked", {}).get("spacecraft", []) if info else []
    if docked:
        add(f"{len(docked)} spacecraft docked", "accent", True)
        now_ts = time.time()
        for craft in sorted(docked, key=lambda c: c.get("docked") or 0):
            kind = (craft.get("mission_type") or "").lower()
            days = int((now_ts - (craft.get("docked") or now_ts)) // 86400)
            add(f"{flag(craft.get('flag_code'), flags)} {(craft.get('name') or '?')[:18]:<18} {kind[:6]:<6} {days:>4}d")
        add()
    period = 2 * math.pi / iss.sat.no_kozai  # minutes
    add("THE STATION", "cyan", True)
    add(f"Orbit #{iss.orbit_number(st['time']):,}")
    add(f"One lap every {period:.1f} minutes")
    add(f"{1440 / period:.1f} sunrises a day")
    add(f"Day {(st['time'] - ISS_LAUNCH).days:,} in orbit")
    add(f"{st['speed'] / 3600:.1f} km every second", "muted")
    return L


PAGES = ["passes", "crew", "station"]


def draw(state):
    cols, rows = shutil.get_terminal_size()
    pal = state["pal"]
    iss, cfg = state["iss"], state["cfg"]
    now = datetime.now(timezone.utc)
    st = iss.at(now)
    scr = Screen(cols, rows, state["mode"])
    panel_w = 40 if cols >= 100 else 0
    map_w = cols - panel_w
    map_h = min(rows - 2, max(4, round(map_w / 4)))  # a 2:1 world, since cells are twice as tall as wide
    top = max(1, (rows - 1 - map_h) // 2)
    draw_map(scr, top, map_w, map_h, state["land"], iss, st, cfg, pal, state["track"])

    # header
    scr.write(1, 0, "ISSQUATCH", pal["accent"], True)
    scr.write(11, 0, "· the ISS, tracked from the woods", pal["muted"])
    stamp = local(now).strftime("%H:%M:%S")
    scr.write(cols - len(stamp) - 1, 0, stamp, pal["muted"])

    if panel_w:
        x = map_w + 2
        live = live_lines(iss, st, pal, next_light_change(iss, now))
        y = 1
        if rows >= 20:  # the squatch, suited up, keeps watch beside the live numbers
            draw_head(scr, x, y, pal)
            for i, (s, c, b) in enumerate(live):
                scr.write(x + 18, y + i, s, c, b)
            y += 9
        else:
            for s, c, b in live:
                scr.write(x, y, s, c, b)
                y += 1
            y += 1
        # the lower half is a page: passes, crew or the station itself
        page = state["page"]
        tx = x
        for i, name in enumerate(PAGES):
            label = f"{i + 1} {name.upper()}"
            scr.write(tx, y, label, pal["cyan"] if name == page else pal["muted"], name == page)
            tx += len(label) + 2
        y += 2
        flags = cfg.get("flags", "emoji")
        body = (passes_lines(st, cfg, state["passes"], pal) if page == "passes" else
                crew_lines(state["info"], pal, flags) if page == "crew" else
                station_lines(iss, st, state["info"], pal, flags))
        for s, c, b in body:
            if y >= rows - 1:
                break
            scr.write(x, y, s[:cols - x - 1], c, b)
            y += 1

    else:  # narrow window: two status lines under the map instead of the panel
        y = min(rows - 3, top + map_h + 1)
        ns, ew = ("N" if st["lat"] >= 0 else "S"), ("E" if st["lon"] >= 0 else "W")
        scr.write(1, y, f"{abs(st['lat']):.1f}°{ns} {abs(st['lon']):.1f}°{ew} · {st['alt']:.0f} km · "
                        f"{st['speed']:,.0f} km/h · {'sunlit' if st['sunlit'] else 'in shadow'}", pal["foreground"])
        passes = state["passes"]
        if "lat" not in cfg:
            line, c = "Set your location for passes: issquatch setup", "muted"
        elif passes:
            p = passes[0]
            line, c = (f"Next visible: {local(p['vis_start']).strftime('%a %H:%M')} "
                       f"(in {fmt_dur((p['vis_start'] - now).total_seconds())}), up to {p['max_el']:.0f}° "
                       f"{p['from']}→{p['to']}"), "accent"
        else:
            line, c = ("No visible passes in the next 3 days" if passes == [] else "Working out passes…"), "muted"
        scr.write(1, y + 1, line, pal[c])

    age = time.time() - iss.fetched
    keys = "q quit · tab/1-3 pages · t track · r refresh"
    scr.write(1, rows - 1, keys, pal["muted"])
    note = f"orbit {fmt_dur(age)} old · CelesTrak" + (" · crew: corquaid" if state["page"] != "passes" else "")
    scr.write(cols - len(note) - 1, rows - 1, note, pal["muted"])
    sys.stdout.write(scr.render(pal["background"]))
    sys.stdout.flush()


# ============================================================ the TUI

def tui():
    cfg = load_config()
    state = {"iss": ISS(), "cfg": cfg, "land": load_land(), "pal": load_theme(), "track": True, "mode": block_mode(cfg),
             "passes": None, "passes_at": 0, "theme_mtime": 0, "info": None,
             "page": "passes" if "lat" in cfg else "crew"}

    def fetch_info():  # crew and docked spacecraft, off the main loop so the map never waits
        while True:
            state["info"] = load_station_info() or state["info"]
            time.sleep(600)

    threading.Thread(target=fetch_info, daemon=True).start()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    resized = [True]
    signal.signal(signal.SIGWINCH, lambda *_: resized.__setitem__(0, True))
    sys.stdout.write("\x1b[?1049h\x1b[?25l\x1b]2;issquatch\x07")
    try:
        tty.setcbreak(fd)
        while True:
            try:
                m = THEME.stat().st_mtime
            except OSError:
                m = 0
            if m != state["theme_mtime"]:  # follow Omarchy theme changes live
                state["pal"], state["theme_mtime"] = load_theme(), m
            if time.time() - state["passes_at"] > 600 or state["passes"] is None:
                draw(state)  # show the map before the (slower) pass search
                state["passes"] = visible_passes(state["iss"], cfg) or ([] if "lat" in cfg else None)
                state["passes_at"] = time.time()
            if resized[0]:
                sys.stdout.write("\x1b[2J")
                resized[0] = False
            draw(state)
            r, _, _ = select.select([sys.stdin], [], [], 1.0)
            if r:
                k = os.read(fd, 16).decode(errors="ignore")
                if k in ("q", "Q", "\x1b"):
                    break
                if k == "t":
                    state["track"] = not state["track"]
                if k == "\t":
                    state["page"] = PAGES[(PAGES.index(state["page"]) + 1) % len(PAGES)]
                if k in ("1", "2", "3"):
                    state["page"] = PAGES[int(k) - 1]
                if k == "r":
                    state["iss"] = ISS(force=True)
                    state["passes"], state["passes_at"] = None, 0
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


# ============================================================ commands

def cmd_now(args):
    iss = ISS()
    st = iss.at(datetime.now(timezone.utc))
    cfg = load_config()
    if "--json" in args:
        out = {k: round(st[k], 4) for k in ("lat", "lon", "alt", "speed")}
        out.update(sunlit=st["sunlit"], orbit=iss.orbit_number(st["time"]), over=over(st["lat"], st["lon"]))
        aboard, _, expedition = crew(load_station_info())
        if aboard:
            out.update(expedition=expedition, crew=[{"name": p.get("name"), "agency": p.get("agency"),
                                                    "country": p.get("country")} for p in aboard])
        passes = visible_passes(iss, cfg, days=2) if "lat" in cfg else None
        if passes:
            out["next_visible_pass"] = {"start": passes[0]["vis_start"].isoformat(), "max_el": round(passes[0]["max_el"]),
                                        "from": passes[0]["from"], "to": passes[0]["to"]}
        print(json.dumps(out))
        return
    ns, ew = ("N" if st["lat"] >= 0 else "S"), ("E" if st["lon"] >= 0 else "W")
    print(f"ISS {abs(st['lat']):.1f}°{ns} {abs(st['lon']):.1f}°{ew} over {over(st['lat'], st['lon'])} · "
          f"{st['alt']:.0f} km · {st['speed']:,.0f} km/h · {'sunlit' if st['sunlit'] else 'in shadow'}")


def cmd_crew(args):
    info = load_station_info(force="--refresh" in args)
    aboard, elsewhere, expedition = crew(info)
    if not info:
        raise SystemExit("issquatch: couldn't load the crew list (no connection and nothing cached).")
    style = load_config().get("flags", "emoji")
    print(f"Expedition {expedition}: {len(aboard)} aboard the ISS\n")
    now_ts = time.time()
    for p in aboard:
        days = int((now_ts - p.get("launched", now_ts)) // 86400)
        print(f"  {flag(p.get('flag_code'), style)} {p.get('name', '?'):<22} {p.get('agency', ''):<10} "
              f"{p.get('position', ''):<16} {p.get('spacecraft', ''):<16} {days}d")
    if elsewhere:
        print(f"\nAlso in space: " + ", ".join(f"{p.get('name')} ({p.get('spacecraft')})" for p in elsewhere))
    docked = info.get("docked", {}).get("spacecraft", [])
    if docked:
        print("\nDocked: " + ", ".join(f"{c.get('name')} ({(c.get('mission_type') or '').lower()})" for c in docked))
    print(f"\n{len(aboard) + len(elsewhere)} people in space right now. Data: corquaid's ISS APIs.")


def cmd_passes(args):
    cfg = load_config()
    if "lat" not in cfg:
        raise SystemExit("Set your location first: issquatch setup")
    iss = ISS()
    days = 7 if "--week" in args else 3
    passes = iss.passes(cfg["lat"], cfg["lon"], days=days, min_el=cfg.get("min_elevation", 10))
    if "--all" not in args:
        passes = [p for p in passes if p["visible"]]
    if not passes:
        print(f"No {'passes' if '--all' in args else 'visible passes'} in the next {days} days from {cfg.get('place', 'here')}.")
        return
    print(f"ISS passes over {cfg.get('place') or '%.2f, %.2f' % (cfg['lat'], cfg['lon'])} (local time)\n")
    print(f"  {'when':<18}{'visible':<10}{'max':>5}  path")
    for p in passes:
        t0 = p["vis_start"] or p["rise"]
        t1 = p["vis_end"] or p["set"]
        vis = fmt_dur((t1 - t0).total_seconds()) if p["visible"] else "not lit"
        print(f"  {local(t0).strftime('%a %d %b %H:%M'):<18}{vis:<10}{p['max_el']:4.0f}°  {p['from']} → {p['to']}")


def geocode(query):
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": query, "format": "json", "limit": 1})
    hits = json.loads(http_get(url))
    if not hits:
        return None
    h = hits[0]
    return float(h["lat"]), float(h["lon"]), h["display_name"].split(",")[0]


def cmd_setup(args):
    cfg = load_config()
    query = " ".join(a for a in args if not a.startswith("-"))
    if not query:
        print("Where are you watching from? A town, an address, or 'lat, lon'.")
        print("(Place names are looked up once with OpenStreetMap's Nominatim; coordinates stay on this machine.)")
        query = input("> ").strip()
    if not query:
        return
    try:
        lat, lon = (float(v) for v in query.replace(" ", "").split(","))
        place = cfg.get("place") if cfg.get("lat") == lat and cfg.get("lon") == lon else f"{lat:.2f}, {lon:.2f}"
    except ValueError:
        found = geocode(query)
        if not found:
            raise SystemExit(f"Couldn't find '{query}'. Try 'lat, lon' instead.")
        lat, lon, place = found
    cfg.update(lat=round(lat, 4), lon=round(lon, 4), place=place)
    cfg.setdefault("notify_minutes", 10)
    cfg.setdefault("min_elevation", 10)
    cfg.setdefault("notify_min_elevation", 20)
    save_config(cfg)
    print(f"Watching from {place} ({lat:.4f}, {lon:.4f}). Saved to {CONFIG}")


def cmd_notify(args):
    """For the systemd timer: one notification per visible pass, `notify_minutes` ahead."""
    cfg = load_config()
    if "lat" not in cfg:
        return
    iss = ISS()
    lead = timedelta(minutes=cfg.get("notify_minutes", 10))
    now = datetime.now(timezone.utc)
    low = cfg.get("notify_min_elevation", 20)  # skip the ones that barely clear the trees
    passes = [p for p in iss.passes(cfg["lat"], cfg["lon"], days=1, min_el=cfg.get("min_elevation", 10))
              if p["visible"] and p["max_el"] >= low]
    seen_file = CACHE / "notified"
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    for p in passes:
        key = p["vis_start"].strftime("%Y%m%dT%H%M")
        if key in seen or not (now <= p["vis_start"] <= now + lead or "--test" in args):
            continue
        mins = max(0, round((p["vis_start"] - now).total_seconds() / 60))
        body = (f"Look {p['from']} at {local(p['vis_start']).strftime('%H:%M')}. It climbs to {p['max_el']:.0f}° "
                f"and heads {p['to']}, visible for {fmt_dur((p['vis_end'] - p['vis_start']).total_seconds())}.")
        subprocess.run(["notify-send", "-a", "issquatch", "-i", "weather-clear-night",
                        f"ISS overhead in {mins} min" if mins else "ISS overhead now", body], check=False)
        if "--test" in args:
            return  # a test shouldn't use up the real notification
        seen.add(key)
    CACHE.mkdir(parents=True, exist_ok=True)
    seen_file.write_text("\n".join(sorted(seen)[-50:]) + "\n")


HELP = f"""issquatch {VERSION}: the ISS, tracked from the woods.

  issquatch                  the live map (tab or 1-3 switch passes/crew/station, t track, r refresh, q quit)
  issquatch now [--json]     where it is right now, in one line
  issquatch crew             who's aboard, with flags, and what's docked
  issquatch passes [--all] [--week]
                             visible passes from your location (--all includes unlit ones)
  issquatch setup [PLACE]    set your location: a place name, or "lat, lon"
  issquatch notify [--test]  notify before a visible pass (the systemd timer runs this)
  issquatch update           fetch fresh orbit data now

Settings: {CONFIG}
"""


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    rest = args[1:]
    if cmd in ("-h", "--help", "help"):
        print(HELP, end="")
    elif cmd in ("-V", "--version"):
        print(VERSION)
    elif cmd == "":
        if not sys.stdin.isatty():
            raise SystemExit("issquatch: the map needs a terminal. Try `issquatch now`.")
        tui()
    elif cmd == "now":
        cmd_now(rest)
    elif cmd == "passes":
        cmd_passes(rest)
    elif cmd == "crew":
        cmd_crew(rest)
    elif cmd == "setup":
        cmd_setup(rest)
    elif cmd == "notify":
        cmd_notify(rest)
    elif cmd == "update":
        iss = ISS(force=True)
        print(f"Orbit data updated (epoch {iss.epoch:%Y-%m-%d %H:%M} UTC).")
    else:
        raise SystemExit(f"issquatch: unknown command '{cmd}'. See issquatch --help.")


if __name__ == "__main__":
    main()
