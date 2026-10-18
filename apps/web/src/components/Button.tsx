import { Button as TDesignButton, type ButtonProps } from 'tdesign-react';
/** Keep native button semantics even when TDesign's disabled default would render a div. */
export function Button(props: ButtonProps) {
  return <TDesignButton {...props} tag="button" />;
}
