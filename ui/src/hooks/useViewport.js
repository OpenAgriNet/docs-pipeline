import { useEffect, useState } from 'react'

export function useViewport(maxWidth = 1080) {
  const getIsCompact = () => (typeof window !== 'undefined' ? window.innerWidth <= maxWidth : false)
  const [isCompact, setIsCompact] = useState(getIsCompact)

  useEffect(() => {
    function onResize() {
      setIsCompact(getIsCompact())
    }

    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [maxWidth])

  return isCompact
}
