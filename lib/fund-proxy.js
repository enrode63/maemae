'use strict';

const MAX_BODY_BYTES = 256 * 1024;
const UPSTREAM_TIMEOUT_MS = 15_000;

const ROUTES = Object.freeze({
  'GET health': '/health',
  'GET status': '/runtime/status',
  'GET results': '/results',
  'GET events': '/events',
  'GET trading-reports': '/automation/reports',
  'GET paper-trades': '/automation/paper-trades',
  'GET decisions': '/automation/decisions',
  'POST chat/conversations': '/chat/conversations',
  'POST chat/send': '/chat/send',
});

function sendJson(response, status, value, extraHeaders = {}) {
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.setHeader('Cache-Control', 'no-store');
  for (const [name, headerValue] of Object.entries(extraHeaders)) {
    response.setHeader(name, headerValue);
  }
  response.end(JSON.stringify(value));
}

function configuredOrigin() {
  const configured = process.env.FUND_API_URL;
  if (!configured) throw new Error('missing_origin');

  const url = new URL(configured);
  if (
    url.protocol !== 'https:' ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash ||
    url.origin !== configured.replace(/\/$/, '')
  ) {
    throw new Error('invalid_origin');
  }
  return url.origin;
}

function requestedPath(request) {
  let value;
  if (typeof request.url === 'string') {
    try {
      const pathname = new URL(request.url, 'http://localhost').pathname;
      const prefix = '/api/fund/';
      if (pathname.startsWith(prefix)) value = pathname.slice(prefix.length);
    } catch {
      return null;
    }
  }
  if (value === undefined) value = request.query && request.query.path;
  const parts = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split('/')
      : [];
  if (!parts.length || parts.some((part) => !part || part === '.' || part === '..' || part.includes('\\'))) {
    return null;
  }
  return parts.join('/');
}

function requestBody(request) {
  if (request.body === undefined || request.body === null || request.body === '') return undefined;
  const body = Buffer.isBuffer(request.body)
    ? request.body
    : Buffer.from(typeof request.body === 'string' ? request.body : JSON.stringify(request.body));
  if (body.length > MAX_BODY_BYTES) throw new Error('body_too_large');
  return body;
}

function createFundProxy(fixedPath) {
  return async function handler(request, response) {
    const method = String(request.method || '').toUpperCase();
    const path = fixedPath || requestedPath(request);

    if (method === 'OPTIONS') {
      if (!path || !Object.keys(ROUTES).some((key) => key.endsWith(` ${path}`))) {
        return sendJson(response, 404, { error: 'not_found' });
      }
      response.statusCode = 204;
      response.setHeader('Allow', 'GET, POST, OPTIONS');
      response.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      response.setHeader('Access-Control-Allow-Headers', 'Content-Type');
      response.setHeader('Access-Control-Max-Age', '600');
      response.setHeader('Cache-Control', 'no-store');
      return response.end();
    }

    const upstreamPath = path && ROUTES[`${method} ${path}`];
    if (!upstreamPath) {
      const pathExists = path && Object.keys(ROUTES).some((key) => key.endsWith(` ${path}`));
      return sendJson(
        response,
        pathExists ? 405 : 404,
        { error: pathExists ? 'method_not_allowed' : 'not_found' },
        pathExists ? { Allow: 'GET, POST, OPTIONS' } : {},
      );
    }

    const team = ['trading-reports', 'paper-trades', 'decisions'].includes(path) && request.query && request.query.team;
    if (team !== undefined && (typeof team !== 'string' || !/^(all|scalping|day|swing|longterm)$/.test(team))) {
      return sendJson(response, 400, { error: 'invalid_team', message: 'invalid team' });
    }

    let origin;
    try {
      origin = configuredOrigin();
    } catch {
      return sendJson(response, 500, { error: 'proxy_not_configured' });
    }

    const token = process.env.FUND_API_TOKEN;
    if (!token || /[\u0000-\u001f\u007f]/.test(token)) {
      return sendJson(response, 500, { error: 'proxy_not_configured' });
    }

    const declaredLength = Number(request.headers && request.headers['content-length']);
    if (Number.isFinite(declaredLength) && declaredLength > MAX_BODY_BYTES) {
      return sendJson(response, 413, { error: 'payload_too_large' });
    }

    let body;
    try {
      body = method === 'POST' ? requestBody(request) : undefined;
    } catch {
      return sendJson(response, 413, { error: 'payload_too_large' });
    }

    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), UPSTREAM_TIMEOUT_MS);
    try {
      const query = typeof team === 'string'
        ? `?team=${encodeURIComponent(team)}` : '';
      const upstream = await fetch(`${origin}${upstreamPath}${query}`, {
        method,
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${token}`,
          ...(body ? { 'Content-Type': 'application/json' } : {}),
        },
        body,
        signal: controller.signal,
      });
      const text = await upstream.text();
      let json = null;
      if (text) {
        try {
          json = JSON.parse(text);
        } catch {
          return sendJson(response, 502, { error: 'invalid_upstream_response' });
        }
      }
      return sendJson(response, upstream.status, json);
    } catch (error) {
      return sendJson(response, error && error.name === 'AbortError' ? 504 : 502, {
        error: error && error.name === 'AbortError' ? 'upstream_timeout' : 'upstream_unavailable',
      });
    } finally {
      clearTimeout(timeout);
    }
  };
}

module.exports = { createFundProxy };
