import { APP_NAME } from '../lib/app-brand'
import { cn } from '../lib/utils'
import { assetUrl } from '../basePath'

/**
 * App mark — square cropped Bharat Vistaar plant icon (public/app-icon.png).
 * Full source artwork is also kept as public/bharat-vistaar-logo-source.png.
 */
export function PlatformLogoIcon({ className, title = APP_NAME }) {
  return (
    <img
      src={assetUrl('app-icon.png')}
      alt={title}
      title={title}
      decoding="async"
      className={cn('size-10 shrink-0 rounded-xl object-contain', className)}
    />
  )
}
