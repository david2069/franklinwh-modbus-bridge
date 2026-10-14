/**
 * Times in the BRIDGE's timezone, labelled (#79).
 *
 * The UI used to format every timestamp with toLocale*() / getHours(), i.e. in
 * whatever zone the viewing device is set to, with no label. A phone or laptop
 * set 2 h ahead of the bridge showed the 18:00 discharge as 20:40 and could
 * not be matched against the logs, the schedules (which run on the bridge's
 * clock) or a peer's records. Everything here formats in the bridge's zone
 * instead and exposes a short label (e.g. "AEDT") to show once per view.
 *
 * Zone source: GET api/system/timezone (`timezone`, an IANA name, plus the
 * bridge's own `abbreviation`). The admin UI already loads it into
 * Alpine.store('app').tz; reading it through the store keeps every x-text
 * that formats a time reactive, so it re-renders when the zone arrives. Pages
 * without Alpine (the /user board) call bridgeTime.setZone() themselves.
 * Until the zone is known this falls back to the browser's zone, and the
 * label says so — never an unlabelled guess.
 *
 * "Gateway zone" = the bridge's zone today: Modbus has no timezone register.
 * A per-gateway zone (gateways.timezone, see the timezone spec) is future work.
 */
(function () {
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const LONG_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
                       'August', 'September', 'October', 'November', 'December'];
  const WEEKDAY_IDX = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };

  let override = null;          // { zone, abbreviation } for pages without Alpine
  const partFmt = new Map();    // zone -> Intl.DateTimeFormat (construction is slow)

  function source() {
    if (override) return override;
    try {
      const tz = window.Alpine && Alpine.store('app') && Alpine.store('app').tz;
      if (tz && tz.timezone) return { zone: tz.timezone, abbreviation: tz.abbreviation };
    } catch (e) { /* store not registered yet */ }
    return null;
  }

  function zone() {
    const s = source();
    return s ? s.zone : undefined;   // undefined = the browser's zone
  }

  function formatter(z) {
    const key = z || '';
    let f = partFmt.get(key);
    if (!f) {
      try {
        f = new Intl.DateTimeFormat('en-GB', {
          timeZone: z, hourCycle: 'h23', weekday: 'short',
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        });
      } catch (e) {
        // An IANA name this browser doesn't know: fall back to its own zone
        // rather than throwing from every x-text on the page.
        f = new Intl.DateTimeFormat('en-GB', {
          hourCycle: 'h23', weekday: 'short',
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit',
        });
      }
      partFmt.set(key, f);
    }
    return f;
  }

  /** Wall-clock fields of epoch-ms `ms` in the bridge's zone. */
  function parts(ms) {
    const out = {};
    for (const p of formatter(zone()).formatToParts(new Date(ms))) out[p.type] = p.value;
    return {
      y: Number(out.year), mo: Number(out.month), d: Number(out.day),
      h: Number(out.hour) % 24, mi: Number(out.minute), s: Number(out.second),
      wd: WEEKDAY_IDX[out.weekday] ?? 0,   // 0 = Sunday, like Date#getDay
    };
  }

  const p2 = (n) => String(n).padStart(2, '0');

  /** Offset of the bridge's zone from UTC at `ms`, in ms. */
  function offsetMs(ms) {
    const t = parts(ms);
    const asUtc = Date.UTC(t.y, t.mo - 1, t.d, t.h, t.mi, t.s);
    return asUtc - Math.floor(ms / 1000) * 1000;
  }

  /** Epoch ms of a wall-clock time in the bridge's zone (month 1-12). Two
   *  passes so a time near a DST change uses the offset in force at it. */
  function fromParts(y, mo, d, h = 0, mi = 0, s = 0) {
    const guess = Date.UTC(y, mo - 1, d, h, mi, s);
    let ms = guess - offsetMs(guess);
    ms = guess - offsetMs(ms);
    return ms;
  }

  const bridgeTime = {
    zone,
    parts,
    offsetMs,
    fromParts,
    setZone(z, abbreviation) { override = z ? { zone: z, abbreviation } : null; },

    /** Short zone label to show once per view, e.g. "AEDT". */
    label(ms = Date.now()) {
      const s = source();
      if (s && s.abbreviation && /^[A-Za-z]{2,6}$/.test(s.abbreviation)) return s.abbreviation;
      try {
        const named = new Intl.DateTimeFormat('en-AU', { timeZone: zone(), timeZoneName: 'short' })
          .formatToParts(new Date(ms)).find((x) => x.type === 'timeZoneName');
        if (named && named.value) return s ? named.value : `${named.value} (this device)`;
      } catch (e) { /* fall through */ }
      return s ? s.zone : 'this device\'s time';
    },

    /** HH:MM, or HH:MM:SS with seconds. */
    time(ms, seconds = false) {
      const t = parts(ms);
      return seconds ? `${p2(t.h)}:${p2(t.mi)}:${p2(t.s)}` : `${p2(t.h)}:${p2(t.mi)}`;
    },
    /** DD/MM */
    dayMonth(ms) { const t = parts(ms); return `${p2(t.d)}/${p2(t.mo)}`; },
    /** "2 Sep" */
    dayMon(ms) { const t = parts(ms); return `${t.d} ${MONTHS[t.mo - 1]}`; },
    /** "October 2026" */
    monthYear(ms) { const t = parts(ms); return `${LONG_MONTHS[t.mo - 1]} ${t.y}`; },
    /** YYYY-MM-DD: a calendar-day key in the bridge's zone. */
    ymd(ms) { const t = parts(ms); return `${t.y}-${p2(t.mo)}-${p2(t.d)}`; },
    /** DD/MM/YYYY HH:MM:SS: the full stamp for titles and tooltips. */
    full(ms) {
      const t = parts(ms);
      return `${p2(t.d)}/${p2(t.mo)}/${t.y} ${p2(t.h)}:${p2(t.mi)}:${p2(t.s)}`;
    },
    /** Same calendar day in the bridge's zone. */
    sameDay(a, b) { return bridgeTime.ymd(a) === bridgeTime.ymd(b); },
    /** Epoch ms of the bridge-zone midnight starting the day that holds `ms`. */
    startOfDay(ms) { const t = parts(ms); return fromParts(t.y, t.mo, t.d); },
    /** "YYYY-MM-DD" shifted by `days` calendar days (zone-free arithmetic). */
    shiftYmd(ymd, days) {
      const [y, m, d] = ymd.split('-').map(Number);
      const t = new Date(Date.UTC(y, m - 1, d + days));
      return `${t.getUTCFullYear()}-${p2(t.getUTCMonth() + 1)}-${p2(t.getUTCDate())}`;
    },
    /** [start, end) epoch SECONDS of the day/week/month/year holding the
     *  calendar day `ymd`, at the bridge's midnights. The aGate's own daily
     *  totals reset at its local midnight, so the span must use that zone.
     *  Weeks start Monday, matching energy_totals.period_start(). */
    periodSpan(ymd, period) {
      const [y, m, d] = ymd.split('-').map(Number);
      let a = [y, m, d], b;
      if (period === 'week') {
        const wd = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
        const mon = new Date(Date.UTC(y, m - 1, d - ((wd + 6) % 7)));
        a = [mon.getUTCFullYear(), mon.getUTCMonth() + 1, mon.getUTCDate()];
        const nxt = new Date(Date.UTC(a[0], a[1] - 1, a[2] + 7));
        b = [nxt.getUTCFullYear(), nxt.getUTCMonth() + 1, nxt.getUTCDate()];
      } else if (period === 'month') {
        a = [y, m, 1];
        b = m === 12 ? [y + 1, 1, 1] : [y, m + 1, 1];
      } else if (period === 'year') {
        a = [y, 1, 1]; b = [y + 1, 1, 1];
      } else {
        const nxt = new Date(Date.UTC(y, m - 1, d + 1));
        b = [nxt.getUTCFullYear(), nxt.getUTCMonth() + 1, nxt.getUTCDate()];
      }
      return [Math.floor(fromParts(...a) / 1000), Math.floor(fromParts(...b) / 1000)];
    },

    // ── The viewer's own zone (owner, 2026-10-15: show both, labelled) ──
    /** True when the device's zone shows a different wall clock than the
     *  bridge's at `ms` (same offset = nothing worth a second reading). */
    viewerDiffers(ms = Date.now()) {
      if (!source()) return false;   // already showing the device's zone
      const local = -new Date(ms).getTimezoneOffset() * 60000;
      return local !== offsetMs(ms);
    },
    /** HH:MM on the viewing device. */
    viewerTime(ms) {
      const d = new Date(ms);
      return `${p2(d.getHours())}:${p2(d.getMinutes())}`;
    },
    /** Short name of the viewing device's zone, e.g. "NZDT". */
    viewerLabel(ms = Date.now()) {
      try {
        const named = new Intl.DateTimeFormat('en-AU', { timeZoneName: 'short' })
          .formatToParts(new Date(ms)).find((x) => x.type === 'timeZoneName');
        if (named && named.value) return named.value;
      } catch (e) { /* fall through */ }
      return Intl.DateTimeFormat().resolvedOptions().timeZone || 'local';
    },
    /** "your time 09:52 NZDT" when the device's zone differs, else ''. */
    viewerNote(ms = Date.now()) {
      return bridgeTime.viewerDiffers(ms)
        ? `your time ${bridgeTime.viewerTime(ms)} ${bridgeTime.viewerLabel(ms)}` : '';
    },

    /** "YYYY-MM-DDTHH:MM" for a datetime-local input, in the bridge's zone. */
    toInput(ms) {
      const t = parts(ms);
      return `${t.y}-${p2(t.mo)}-${p2(t.d)}T${p2(t.h)}:${p2(t.mi)}`;
    },
    /** Parse a datetime-local value as a time in the bridge's zone. */
    fromInput(value) {
      const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(value || '');
      if (!m) return NaN;
      return fromParts(+m[1], +m[2], +m[3], +m[4], +m[5], +(m[6] || 0));
    },
  };

  window.bridgeTime = bridgeTime;
})();
