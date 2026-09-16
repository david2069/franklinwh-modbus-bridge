/** Tailwind build config.
 *
 * Replaces the 403 KB Play/CDN runtime, which compiled classes IN THE BROWSER
 * via a MutationObserver that re-scanned the whole DOM on every mutation. With
 * all eight tabs permanently mounted (79% of the DOM inert at any moment) that
 * scan was paid on every timer tick, on a phone, forever.
 *
 * Content globs must cover every place a class name can appear. They are all
 * literals here — no `text-${c}-400` anywhere in the codebase — so the scanner
 * finds them without a safelist. If that ever changes, the style silently
 * vanishes rather than erroring, so add a safelist entry at the same time.
 */
module.exports = {
  content: [
    './src/franklinwh_bridge/templates/**/*.html',
    './src/franklinwh_bridge/static/js/*.js',
  ],
  // Theming is data-theme + CSS custom properties, not Tailwind's dark:
  // variant — no dark: utility exists in the codebase. Kept only because the
  // old inline config set it, and removing it changes nothing.
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        primary: {
          DEFAULT: 'var(--primary-color)',
          dark: 'var(--primary-dark)',
          light: 'var(--primary-light)',
        },
      },
    },
  },
};
