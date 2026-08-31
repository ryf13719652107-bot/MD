import type { ReactNode } from 'react';
import { X } from 'lucide-react';

export type NoticeKind = 'error' | 'success' | 'info' | 'warn';

const KIND_CLASS: Record<NoticeKind, string> = {
  error: 'bg-red-950/40 border-red-800 text-red-200',
  success: 'bg-emerald-950/40 border-emerald-800 text-emerald-200',
  info: 'bg-blue-950/40 border-blue-800 text-blue-200',
  warn: 'bg-amber-950/40 border-amber-800 text-amber-200',
};

export default function InlineNotice({
  kind,
  children,
  onClose,
}: {
  kind: NoticeKind;
  children: ReactNode;
  onClose?: () => void;
}) {
  return (
    <div className={`flex items-start gap-2 border rounded-lg px-3 py-2 text-sm ${KIND_CLASS[kind]}`}>
      <div className="flex-1 min-w-0 whitespace-pre-wrap">{children}</div>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="shrink-0 p-0.5 rounded hover:bg-white/10"
          aria-label="关闭"
        >
          <X size={14} />
        </button>
      )}
    </div>
  );
}
