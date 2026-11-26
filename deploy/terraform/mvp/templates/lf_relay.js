// #129 / ADR-0029: viewer-request function for the /lf-otel/* relay to Langfuse Cloud US.
// The IPv6-only host cannot reach Langfuse (IPv4 only, F-078), so it posts OTLP spans here.
// Only POSTs to the OTLP path with the secret header pass; the header is removed and the
// /lf-otel prefix stripped before the request reaches Langfuse (RL-1). The secret is in the
// associated KeyValueStore (written by scripts/aws_mvp.py relay-key), never in this code.
import cf from 'cloudfront';

const kvs = cf.kvs();
const HEADER = 'x-aorte-relay-key';
const PREFIX = '/lf-otel';
const OTLP_PATH = '/lf-otel/api/public/otel/';

function forbidden() {
  return { statusCode: 403, statusDescription: 'Forbidden' };
}

async function handler(event) {
  const request = event.request;
  const given = request.headers[HEADER] ? request.headers[HEADER].value : '';
  delete request.headers[HEADER];
  if (request.method !== 'POST' || !request.uri.startsWith(OTLP_PATH) || !given) {
    return forbidden();
  }
  let expected = '';
  try {
    expected = await kvs.get('relay-key');
  } catch (err) {
    return forbidden(); // no key yet: fail closed
  }
  if (!expected || expected === 'unset' || given !== expected) {
    return forbidden();
  }
  request.uri = request.uri.substring(PREFIX.length);
  return request;
}
