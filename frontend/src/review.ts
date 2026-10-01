import { fullDate } from './format'

export const STEP_NAMES = ['Source', 'Map columns', 'Defaults', 'Duplicates', 'Review']

export const ORIGINS: Record<string, string> = { upload: 'Upload', backfill: 'Drive archive', tiller: 'Tiller', simplefin: 'SimpleFIN' }

export const STATUS_TAG: Record<string, string> = {
  extracting: 'tag-outline', mapping: 'tag-outline', preparing: 'tag-outline', review: 'tag-outline',
  committed: 'tag-accent', rolled_back: 'tag-neutral', failed: 'tag-neutral',
}

export function pairConfidence(score: string, ai: string | null): string {
  const n = ai !== null ? Number(ai) : Number(score)
  return `${Math.round(n * 100)}%${ai !== null ? ' AI' : ''} match`
}

export const briefFacts = (t: { txn_date: string; account_name: string | null; created_at: string; source_type: string }) =>
  [
    ['Date', fullDate(t.txn_date)],
    ['Account', t.account_name ?? '—'],
    ['Added', `${fullDate(t.created_at)} via ${t.source_type}`],
  ] as [string, string][]
