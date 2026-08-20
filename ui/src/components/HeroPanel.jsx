import { BookOpenText, Languages, ScanText } from 'lucide-react'
import { PlatformLogoIcon } from './PlatformLogoIcon'
import { APP_NAME } from '../lib/app-brand'

const HIGHLIGHTS = [
  {
    icon: ScanText,
    title: 'Read any document',
    description: 'Turns scans and photos into clean, readable text.',
  },
  {
    icon: Languages,
    title: 'Understand any language',
    description: 'Translate documents and check them against the original.',
  },
  {
    icon: BookOpenText,
    title: 'Find answers fast',
    description: 'Search across every document and get the exact answer you need.',
  },
]

/**
 * Login left panel — solid brand color with copy, replacing the old
 * full-bleed illustration so the panel stays legible and on-brand.
 */
export function HeroPanel() {
  return (
    <aside className="relative hidden h-full min-h-svh overflow-hidden bg-[#0f3d33] lg:block">
      <div
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            'radial-gradient(120% 100% at 0% 0%, rgba(16,185,129,0.28) 0%, rgba(15,61,51,0) 55%), radial-gradient(120% 100% at 100% 100%, rgba(5,150,105,0.22) 0%, rgba(15,61,51,0) 55%)',
        }}
      />

      <div className="relative flex h-full min-h-svh flex-col justify-between px-14 py-14 xl:px-20">
        <div className="flex items-center gap-3">
          <PlatformLogoIcon className="size-11 rounded-xl shadow-md" title={APP_NAME} />
          <span className="text-lg font-semibold tracking-tight text-white">{APP_NAME}</span>
        </div>

        <div className="max-w-md">
          <h2 className="text-4xl font-semibold leading-tight tracking-tight text-white">
            Find anything in your documents, instantly.
          </h2>
          <p className="mt-4 text-base leading-relaxed text-emerald-100/80">
            One place to bring in your documents, translate them, and make them
            easy to search — so your team always finds the right answer.
          </p>

          <div className="mt-10 space-y-5">
            {HIGHLIGHTS.map(({ icon: Icon, title, description }) => (
              <div key={title} className="flex items-start gap-3.5">
                <div className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-lg bg-white/10">
                  <Icon className="size-4.5 text-emerald-200" />
                </div>
                <div>
                  <p className="text-sm font-medium text-white">{title}</p>
                  <p className="text-sm leading-relaxed text-emerald-100/70">{description}</p>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div />
      </div>
    </aside>
  )
}
