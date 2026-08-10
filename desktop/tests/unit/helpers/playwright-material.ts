import type { FingerprintResult } from '../../../src/main/browser/playwright-snapshot';

export function attestedMaterial(
  material: string,
  options: {
    tag?: string;
    inputType?: string;
    tier?: string;
    contentEditable?: boolean;
  } = {},
): string {
  const {
    tag = 'button',
    inputType = 'button',
    tier = 'plain',
    contentEditable = false,
  } = options;
  return [
    material,
    `attested-tag\0${tag}`,
    `attested-input-type\0${inputType}`,
    `attested-content-editable\0${contentEditable}`,
    `attested-field-tier\0${tier}`,
  ].join('\n');
}

export function materialState(
  material: string,
  overrides: Partial<Omit<FingerprintResult, 'security'>> = {},
): Record<string, unknown> {
  const tag = overrides.tag ?? 'button';
  const inputType = overrides.inputType ?? 'button';
  return {
    material,
    navigation: '',
    downloadNavigation: '',
    action: '',
    actionKind: 'activate',
    accessibleRole: 'button',
    accessibleName: 'Submit',
    documentBaseURI: 'https://example.test/',
    documentURL: 'https://example.test/',
    tag,
    inputType,
    contentEditable: false,
    fieldProbe: {
      type: inputType,
      autocomplete: '',
      name: '',
      id: '',
      placeholder: '',
      ariaLabel: '',
      labelText: '',
    },
    complete: true,
    ...overrides,
  };
}
