// Display helpers. Times are shown in the viewer's local time zone.

export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${seconds}`
    : `${minutes}:${seconds}`;
}

export function duration(seconds: number | null | undefined): string {
  return seconds == null ? '' : clock(seconds * 1000);
}

export function timeOfDay(iso: string | null): string {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

export function dayKey(iso: string | null): string {
  return iso ? new Date(iso).toDateString() : 'Unknown date';
}

export function dayLabel(iso: string | null): string {
  if (!iso) return 'Unknown date';
  const date = new Date(iso);
  const today = new Date();
  const yesterday = new Date(today.getTime() - 86_400_000);
  if (date.toDateString() === today.toDateString()) return 'Today';
  if (date.toDateString() === yesterday.toDateString()) return 'Yesterday';
  return date.toLocaleDateString([], {
    weekday: 'short', day: 'numeric', month: 'short',
    year: date.getFullYear() === today.getFullYear() ? undefined : 'numeric',
  });
}

export function dateTime(iso: string | null): string {
  if (!iso) return '';
  return `${dayLabel(iso)}, ${timeOfDay(iso)}`;
}
