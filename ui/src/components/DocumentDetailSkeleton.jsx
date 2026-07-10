import React from 'react'
import { Skeleton } from './ui/skeleton'

export function DocumentDetailSkeleton() {
  return (
    <div className="flex h-[calc(100vh-3rem)] flex-col">
      <div className="shrink-0 border-b border-border bg-card px-4 py-3">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-3">
            <Skeleton className="h-8 w-8 rounded-md" />
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <Skeleton className="h-6 w-72" />
              <Skeleton className="h-4 w-96" />
            </div>
          </div>
          <Skeleton className="h-10 w-full rounded-lg" />
        </div>
      </div>
      <div className="flex min-h-0 flex-1">
        <div className="hidden w-[380px] shrink-0 border-r border-border lg:block">
          <Skeleton className="h-full w-full rounded-none" />
        </div>
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="border-b border-border px-4 py-3">
            <Skeleton className="h-10 w-[28rem] rounded-lg" />
          </div>
          <div className="flex flex-col gap-4 p-4">
            <Skeleton className="h-36 rounded-lg" />
            <Skeleton className="h-56 rounded-lg" />
          </div>
        </div>
      </div>
    </div>
  )
}
