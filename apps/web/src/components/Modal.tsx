import { Button } from './Button';
import { useEffect, useId, type PropsWithChildren } from 'react';
import { Dialog, type DialogProps } from 'tdesign-react';
import { CloseIcon } from 'tdesign-icons-react';
/** Accessible modal boundary shared by all TDesign dialogs. */
export function Modal({
  label,
  closeDisabled = false,
  ...props
}: PropsWithChildren<DialogProps & { label: string; closeDisabled?: boolean }>) {
  const id = useId();
  useEffect(() => {
    if (!props.visible) return;
    const previous = document.activeElement as HTMLElement | null;
    const root = () => document.querySelector<HTMLElement>(`[data-cloud-dialog="${id}"]`);
    const focusable = () =>
      Array.from(
        root()?.querySelectorAll<HTMLElement>(
          'button:not([disabled]),input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex="0"]',
        ) ?? [],
      ).filter((node) => node.offsetParent !== null);
    const timer = setTimeout(() => focusable()[0]?.focus(), 0);
    const trap = (event: KeyboardEvent) => {
      if (event.key !== 'Tab') return;
      const elements = focusable();
      const first = elements[0];
      const last = elements.at(-1);
      if (!first) {
        event.preventDefault();
        return;
      }
      if (!root()?.contains(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', trap);
    return () => {
      clearTimeout(timer);
      document.removeEventListener('keydown', trap);
      if (previous?.isConnected) previous.focus();
    };
  }, [props.visible, id]);
  const attributes = {
    role: 'dialog',
    'aria-modal': true,
    'aria-hidden': !props.visible,
    'aria-label': label,
    'data-cloud-dialog': id,
  };
  return (
    <Dialog
      {...attributes}
      {...props}
      closeBtn={false}
      header={
        <div className="modal-heading">
          <span>{props.header ?? label}</span>
          <Button
            variant="text"
            shape="square"
            aria-label="关闭"
            disabled={closeDisabled}
            icon={<CloseIcon />}
            onClick={(e) => props.onClose?.({ trigger: 'close-btn', e })}
          />
        </div>
      }
    />
  );
}
