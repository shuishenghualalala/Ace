// Public integration boundary for structured desktop replies.
export {parseVisual} from './rich-reply/protocol';
export {bindNativeVisuals,hydrateNativeVisuals,renderNativeVisual} from './rich-reply/render';
export type {NativeVisual,ReplySpec as VisualSpec} from './rich-reply/types';
