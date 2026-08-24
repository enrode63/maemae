'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { createFundProxy } = require('./fund-proxy');

test('trading reports proxy rejects an invalid team before contacting upstream', async () => {
  const originalFetch = global.fetch;
  let fetched = false;
  global.fetch = async () => {
    fetched = true;
    throw new Error('must not fetch');
  };
  const response = {
    headers: {},
    setHeader(name, value) { this.headers[name] = value; },
    end(body) { this.body = body; },
  };

  try {
    await createFundProxy('trading-reports')(
      { method: 'GET', query: { team: 'bogus' }, headers: {} },
      response,
    );
  } finally {
    global.fetch = originalFetch;
  }

  assert.equal(response.statusCode, 400);
  assert.deepEqual(JSON.parse(response.body), { error: 'invalid_team', message: 'invalid team' });
  assert.equal(fetched, false);
});
