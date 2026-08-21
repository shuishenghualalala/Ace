import { describe, expect, it } from 'vitest';
import {
  WELCOME_COPY_POOL,
  welcomeCopyForDate,
} from '../../src/ui/features/scenarios-hub';

describe('Welcome daily copy', () => {
  it('keeps the six approved Crew greetings', () => {
    expect(WELCOME_COPY_POOL).toEqual([
      { title: '忙点好，忙点好。', subtitle: '说吧，今天又忙点啥？' },
      { title: '天知地知，你知我知。', subtitle: '你就是最忙的牛马——今天先忙哪件？' },
      { title: '一打开 Crew，我就知道你又有活了。', subtitle: '来吧，先从哪件开始？' },
      { title: '活是一点没少。', subtitle: '不过没事，我陪你一起干。' },
      { title: '来都来了。', subtitle: '咸鱼准备翻身了。' },
      { title: '又见面了，小牛马。', subtitle: '今天先解决哪件麻烦事？' },
    ]);
  });

  it('is stable within one local day and rotates across six days', () => {
    expect(welcomeCopyForDate(new Date(2026, 7, 10, 0, 1))).toEqual(
      welcomeCopyForDate(new Date(2026, 7, 10, 23, 59)),
    );

    const sixDays = Array.from({ length: 6 }, (_, offset) =>
      welcomeCopyForDate(new Date(2026, 7, 10 + offset)).title);
    expect(new Set(sixDays).size).toBe(6);
  });

  it('falls back to the first greeting for an invalid date', () => {
    expect(welcomeCopyForDate(new Date(Number.NaN))).toEqual(WELCOME_COPY_POOL[0]);
  });
});
