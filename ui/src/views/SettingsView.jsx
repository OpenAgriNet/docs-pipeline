import React, { useEffect, useMemo, useState } from 'react'
import {
  AlertCircle,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  Clock,
  Database,
  Filter,
  History,
  ListOrdered,
  RotateCcw,
  Save,
  Search,
  Shield,
  SlidersHorizontal,
  Sparkles,
} from 'lucide-react'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '../components/ui/alert-dialog'
import { Button } from '../components/ui/button'
import { Input } from '../components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select'
import { Skeleton } from '../components/ui/skeleton'
import { fetchJson, formatCompactDateTime } from '../lib/pipelineUi'
import { useAuth } from '../auth/AuthProvider'
import { cn } from '../lib/utils'
import settingsPageSchema from '../config/settings-page.json'

const SECTION_ICONS = {
  Search,
  ListOrdered,
  Database,
  Sparkles,
}

function SettingsNotice({ tone = 'warning', message }) {
  const classes =
    tone === 'success'
      ? 'border-[#059669]/25 bg-[#059669]/8 text-[#047857]'
      : tone === 'error'
        ? 'border-red-200 bg-red-50 text-red-700'
        : 'border-amber-200 bg-amber-50 text-amber-800'

  const Icon = tone === 'success' ? CheckCircle : AlertCircle

  return (
    <div className={cn('rounded-xl border px-3.5 py-2.5 text-sm', classes)}>
      <div className="flex items-center gap-2">
        <Icon className="h-4 w-4 shrink-0" />
        <span>{message}</span>
      </div>
    </div>
  )
}

function Toggle({ checked, onChange, disabled }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        'relative h-6 w-11 shrink-0 rounded-full transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#059669]/30 focus-visible:ring-offset-2',
        'disabled:cursor-not-allowed disabled:opacity-50',
        checked ? 'bg-[#059669]' : 'bg-[#d5e0db]',
      )}
    >
      <span
        className={cn(
          'absolute top-0.5 left-0.5 block h-5 w-5 rounded-full bg-white shadow-sm transition-transform',
          checked && 'translate-x-5',
        )}
      />
    </button>
  )
}

function CustomSelect({ value, options, disabled, onChange, placeholder = 'Select…' }) {
  const items = useMemo(() => {
    const list = (options || []).map((opt) =>
      typeof opt === 'string' ? { value: opt, label: opt } : opt,
    )
    // Ensure current value is present even if not in schema options
    if (value != null && value !== '' && !list.some((o) => String(o.value) === String(value))) {
      list.push({ value: String(value), label: String(value) })
    }
    return list
  }, [options, value])

  return (
    <Select
      value={value != null && value !== '' ? String(value) : undefined}
      onValueChange={onChange}
      disabled={disabled}
    >
      <SelectTrigger
        className={cn(
          'h-9 w-full max-w-[220px] rounded-lg border-[#d5e0db] bg-white px-3 text-sm text-[#14201b] shadow-none',
          'focus:ring-2 focus:ring-[#059669]/25 focus:ring-offset-0',
          'disabled:cursor-not-allowed disabled:bg-[#f7faf8] disabled:opacity-70',
          'data-[placeholder]:text-[#5f7269]',
        )}
      >
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent
        className={cn(
          'z-50 rounded-xl border border-[#d5e0db] bg-white p-1 text-[#14201b] shadow-lg',
          'data-[state=open]:animate-in data-[state=closed]:animate-out',
        )}
      >
        {items.map((opt) => (
          <SelectItem
            key={opt.value}
            value={String(opt.value)}
            className={cn(
              'cursor-pointer rounded-lg py-2 pl-8 pr-3 text-sm text-[#14201b]',
              'focus:bg-[#d5e0db]/70 focus:text-[#14201b]',
              'data-[state=checked]:bg-[#d5e0db] data-[state=checked]:font-medium',
            )}
          >
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

function FieldControl({ field, value, disabled, onChange }) {
  if (field.type === 'select') {
    return (
      <CustomSelect
        value={value}
        options={field.options}
        disabled={disabled}
        onChange={onChange}
        placeholder={`Select ${field.label.toLowerCase()}`}
      />
    )
  }

  if (field.type === 'boolean') {
    return <Toggle checked={Boolean(value)} disabled={disabled} onChange={onChange} />
  }

  return (
    <Input
      type={field.type}
      step={field.step}
      value={value ?? ''}
      disabled={disabled}
      onChange={(event) =>
        onChange(field.type === 'number' ? Number(event.target.value) : event.target.value)
      }
      className={cn(
        'h-9 w-full max-w-[220px] rounded-lg border-[#d5e0db] bg-white px-3 text-sm text-[#14201b] shadow-none',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#059669]/25 focus-visible:border-[#059669]/40',
        'disabled:cursor-not-allowed disabled:bg-[#f7faf8] disabled:text-[#5f7269]',
      )}
    />
  )
}

function SettingsSection({ icon: Icon, title, description, children }) {
  return (
    <section className="overflow-hidden rounded-2xl border border-[#d5e0db] bg-white shadow-sm">
      <div className="flex items-start gap-3 border-b border-[#d5e0db]/80 bg-[#f7faf8]/80 px-5 py-4">
        <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-[#d5e0db]/70 text-[#047857]">
          <Icon className="size-4" strokeWidth={1.9} />
        </div>
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-[#14201b]">{title}</h2>
          <p className="mt-0.5 text-xs leading-relaxed text-[#5f7269]">{description}</p>
        </div>
      </div>
      <div className="divide-y divide-[#e8efeb]">{children}</div>
    </section>
  )
}

function SettingsRow({ label, help, children }) {
  return (
    <div className="flex flex-col gap-3 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0 flex-1">
        <label className="text-sm font-medium text-[#14201b]">{label}</label>
        {help ? <p className="mt-1 text-xs leading-relaxed text-[#5f7269]">{help}</p> : null}
      </div>
      <div className="w-full sm:w-auto sm:shrink-0 sm:flex sm:justify-end">{children}</div>
    </div>
  )
}

export default function SettingsView() {
  const { hasPermission } = useAuth()
  const canAdmin = hasPermission(settingsPageSchema.page.permission || 'admin')
  const defaults = settingsPageSchema.defaults
  const sections = settingsPageSchema.sections

  const [settings, setSettings] = useState(defaults)
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [saveSuccess, setSaveSuccess] = useState(false)
  const [showHistory, setShowHistory] = useState(true)
  const [history, setHistory] = useState([])
  const [showResetConfirm, setShowResetConfirm] = useState(false)

  useEffect(() => {
    fetchSettings()
  }, [])

  async function fetchSettings() {
    setError('')
    try {
      const [data, historyData] = await Promise.all([
        fetchJson(settingsPageSchema.apis.get),
        fetchJson(settingsPageSchema.apis.audit),
      ])
      setSettings({ ...defaults, ...data })
      setHistory(historyData.logs || [])
      setDirty(false)
    } catch (loadError) {
      setError(loadError.message)
    } finally {
      setLoading(false)
    }
  }

  function update(key, value) {
    setSettings((current) => ({ ...current, [key]: value }))
    setDirty(true)
    setSaveSuccess(false)
  }

  async function handleSave() {
    setError('')
    setSaving(true)
    try {
      const data = await fetchJson(settingsPageSchema.apis.put, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      })
      setSettings({ ...defaults, ...data })
      setDirty(false)
      setSaveSuccess(true)
      fetchSettings()
    } catch (saveError) {
      setError(saveError.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleReset() {
    setError('')
    try {
      const data = await fetchJson(settingsPageSchema.apis.reset, { method: 'POST' })
      setSettings({ ...defaults, ...data })
      setDirty(false)
      setSaveSuccess(false)
      fetchSettings()
    } catch (resetError) {
      setError(resetError.message)
    }
  }

  if (loading) {
    return (
      <div className="mx-auto max-w-4xl space-y-5 p-6">
        <div className="space-y-2">
          <Skeleton className="h-8 w-40 rounded-lg" />
          <Skeleton className="h-4 w-64 rounded-md" />
        </div>
        {Array.from({ length: 3 }).map((_, index) => (
          <Skeleton key={index} className="h-44 rounded-2xl" />
        ))}
      </div>
    )
  }

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6 pb-28">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="mb-2 inline-flex items-center gap-2 rounded-full border border-[#d5e0db] bg-[#f7faf8] px-2.5 py-1 text-[11px] font-medium text-[#5f7269]">
            <SlidersHorizontal className="size-3.5" />
            {settingsPageSchema.page.eyebrow}
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-[#14201b]">
            {settingsPageSchema.page.title}
          </h1>
          <p className="mt-1 max-w-xl text-sm leading-relaxed text-[#5f7269]">
            {settingsPageSchema.page.description}
          </p>
        </div>

        {!canAdmin ? (
          <div className="inline-flex items-center gap-2 self-start rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800">
            <Shield className="size-3.5" />
            Read-only — admin required to edit
          </div>
        ) : null}
      </div>

      {(dirty || saveSuccess || error) && (
        <div className="space-y-2">
          {dirty ? <SettingsNotice message="You have unsaved changes on this page." /> : null}
          {saveSuccess ? <SettingsNotice tone="success" message="Settings saved successfully." /> : null}
          {error ? <SettingsNotice tone="error" message={error} /> : null}
        </div>
      )}

      {sections.map((section) => {
        const Icon = SECTION_ICONS[section.icon] || SlidersHorizontal
        return (
          <SettingsSection
            key={section.id}
            icon={Icon}
            title={section.title}
            description={section.description}
          >
            {section.fields.map((field) => (
              <SettingsRow key={field.key} label={field.label} help={field.help}>
                <FieldControl
                  field={field}
                  value={settings[field.key]}
                  disabled={!canAdmin}
                  onChange={(value) => update(field.key, value)}
                />
              </SettingsRow>
            ))}
          </SettingsSection>
        )
      })}

      <section className="overflow-hidden rounded-2xl border border-[#d5e0db] bg-white shadow-sm">
        <button
          type="button"
          className="flex w-full items-center justify-between gap-3 bg-[#f7faf8]/80 px-5 py-4 text-left transition hover:bg-[#f0f5f2]"
          onClick={() => setShowHistory((value) => !value)}
        >
          <div className="flex items-center gap-3">
            <div className="flex size-9 items-center justify-center rounded-xl bg-[#d5e0db]/70 text-[#047857]">
              <History className="size-4" />
            </div>
            <div>
              <h2 className="text-sm font-semibold text-[#14201b]">
                {settingsPageSchema.history.title}
              </h2>
              <p className="text-xs text-[#5f7269]">
                {history.length ? `${history.length} recent audit entries` : 'No audited changes yet'}
              </p>
            </div>
          </div>
          {showHistory ? (
            <ChevronUp className="size-4 text-[#5f7269]" />
          ) : (
            <ChevronDown className="size-4 text-[#5f7269]" />
          )}
        </button>

        {showHistory ? (
          history.length ? (
            <div className="divide-y divide-[#e8efeb]">
              {history.map((entry) => (
                <div
                  key={entry.id}
                  className="flex flex-col gap-2 px-5 py-3.5 sm:flex-row sm:items-center sm:gap-4"
                >
                  <div className="flex min-w-0 flex-1 items-start gap-2.5">
                    <Clock className="mt-0.5 size-3.5 shrink-0 text-[#5f7269]" />
                    <div className="min-w-0">
                      <div className="font-mono text-xs font-semibold text-[#14201b]">
                        {entry.field_name || entry.field || 'setting'}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs">
                        <span className="rounded-md bg-[#f7faf8] px-1.5 py-0.5 text-[#5f7269] line-through">
                          {entry.old_value ?? '—'}
                        </span>
                        <span className="text-[#5f7269]">→</span>
                        <span className="rounded-md bg-[#d5e0db]/60 px-1.5 py-0.5 font-medium text-[#14201b]">
                          {entry.new_value ?? '—'}
                        </span>
                      </div>
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-3 pl-6 text-xs text-[#5f7269] sm:pl-0">
                    <span className="font-medium">{entry.actor || 'system'}</span>
                    <span>{formatCompactDateTime(entry.timestamp)}</span>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="px-5 py-10 text-center">
              <Filter className="mx-auto size-8 text-[#d5e0db]" />
              <p className="mt-2 text-sm text-[#5f7269]">No settings audit entries yet.</p>
            </div>
          )
        ) : null}
      </section>

      <div className="fixed bottom-0 left-0 right-0 z-20 border-t border-[#d5e0db] bg-white/95 backdrop-blur-sm">
        <div className="mx-auto flex max-w-4xl items-center justify-between gap-3 px-6 py-3">
          <p className="hidden text-xs text-[#5f7269] sm:block">
            {dirty
              ? 'Unsaved changes will apply to future search queries.'
              : 'All search settings are up to date.'}
          </p>
          <div className="flex w-full items-center justify-end gap-2 sm:w-auto">
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={!canAdmin}
              onClick={() => setShowResetConfirm(true)}
              className="rounded-lg border-[#d5e0db] bg-white text-[#14201b] hover:bg-[#f7faf8]"
            >
              <RotateCcw className="mr-1.5 size-3.5" />
              Reset
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!dirty || !canAdmin || saving}
              onClick={handleSave}
              className="rounded-lg bg-[#059669] text-white hover:bg-[#047857] disabled:opacity-50"
            >
              <Save className="mr-1.5 size-3.5" />
              {saving ? 'Saving…' : 'Save changes'}
            </Button>
          </div>
        </div>
      </div>

      <AlertDialog open={showResetConfirm} onOpenChange={setShowResetConfirm}>
        <AlertDialogContent className="rounded-2xl border-[#d5e0db]">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-[#14201b]">Reset to defaults?</AlertDialogTitle>
            <AlertDialogDescription className="text-[#5f7269]">
              This restores all search settings to factory defaults. Recent values will remain in the audit history.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="rounded-lg border-[#d5e0db]">Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleReset}
              className="rounded-lg bg-[#059669] text-white hover:bg-[#047857]"
            >
              Reset all
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
