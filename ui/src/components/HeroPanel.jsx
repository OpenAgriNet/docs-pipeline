/**
 * Login left panel — full-bleed smart-agriculture visual.
 * Asset: public/login-hero.png
 */
export function HeroPanel() {
  return (
    <aside
      className="relative hidden min-h-svh flex-1 overflow-hidden bg-[#7ec8e3] lg:block"
      aria-hidden="true"
    >
      <img
        src="/login-hero.png"
        alt=""
        // Landscape art is cropped on tall panels; bias left so the farmer stays visible.
        className="absolute inset-0 h-full w-full object-cover object-left"
        style={{ objectPosition: '18% center' }}
        decoding="async"
      />
      {/* Soft edge into the form panel */}
      <div className="pointer-events-none absolute inset-y-0 right-0 w-24 bg-gradient-to-l from-black/10 to-transparent" />
    </aside>
  )
}
