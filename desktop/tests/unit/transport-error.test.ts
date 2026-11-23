/** @vitest-environment happy-dom */

import { expect, it } from 'vitest';
import { readJsonResponse } from '../../src/ui/api/transport';

it('keeps the HTTP status on JSON error responses', async () => {
  const response = new Response(JSON.stringify({ error: '需要管理员权限' }), {
    status: 403,
    headers: { 'content-type': 'application/json' },
  });
  await expect(readJsonResponse(response, '/api/mcp/servers')).rejects.toMatchObject({
    message: '需要管理员权限',
    status: 403,
  });
});
