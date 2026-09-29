const PRIVATE_KEY_BLOCK = /-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----[\s\S]*?-----END(?: [A-Z0-9]+)* PRIVATE KEY-----/gi;
const URI_CREDENTIALS = /\b([a-z][a-z0-9+.-]*:\/\/)[^\s/@:]+:[^\s/@]+@/gi;
// A header line carries the credential in its whole value, whatever the scheme
// (Bearer, Basic, Token, Digest, or none), so the entire remainder of the line is dropped.
const AUTHORIZATION_HEADER_LINE = /(^|\n)(\s*(?:proxy-)?authorization\s*:\s*)[^\r\n]+/gi;
const AUTHORIZATION_VALUE = /\b((?:authorization|proxy-authorization)\s*[:=]\s*)(?:bearer|basic)\s+[A-Za-z0-9+/._~=-]+/gi;
const FLAG_SECRET = /(\s--?(?:api[-_]?key|access[-_]?token|auth[-_]?token|client[-_]?secret|password|passwd|secret|token)(?:=|\s+))(?:"[^"]*"|'[^']*'|[^\s]+)/gi;
const XML_SECRET = /(<(?:password|passwd|secret|token|api[-_]?key|client[-_]?secret)>)[\s\S]*?(<\/(?:password|passwd|secret|token|api[-_]?key|client[-_]?secret)>)/gi;
const JSON_SECRET = /(["'](?:api[-_]?key|access[-_]?key|access[-_]?token|auth[-_]?token|(?:proxy-)?authorization|client[-_]?secret|private[-_]?key|password|passwd|pwd|refresh[-_]?token|secret|session[-_]?token|token)["']\s*:\s*)(?:"[^"]*"|'[^']*'|[^\s,;\]}]+)/gi;
// `authorization` is listed here as well as in AUTHORIZATION_VALUE: that pattern only
// matches `bearer`/`basic` schemes, so a bare `Authorization: <opaque-token>` header
// (common with API gateways and custom schemes) would otherwise reach the model intact.
const ASSIGNMENT_SECRET = /(\b(?:api[-_]?key|access[-_]?key|access[-_]?token|auth[-_]?token|(?:proxy-)?authorization|client[-_]?secret|private[-_]?key|password|passwd|pwd|refresh[-_]?token|secret|session[-_]?token|token)\b\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;\]}]+)/gi;
const ENV_SECRET = /(^|\n)(\s*(?:export\s+)?[A-Z0-9_]*(?:API_KEY|ACCESS_KEY|ACCESS_TOKEN|AUTH_TOKEN|CLIENT_SECRET|PASSWORD|PASSWD|PRIVATE_KEY|REFRESH_TOKEN|SECRET|SESSION_TOKEN|TOKEN)[A-Z0-9_]*\s*=\s*)([^\r\n]*)/gi;
const KNOWN_TOKEN = /\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{16,}|github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{24,}|npm_[A-Za-z0-9]{20,})\b/g;
const JWT = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;
const HIGH_ENTROPY_TOKEN = /\b[A-Za-z0-9_-]{32,}\b/g;

const REDACTED = "<redacted>";

/**
 * Conservative, deterministic defense-in-depth redaction for IDE context.
 * Sensitive files are rejected separately; this catches credentials embedded
 * in otherwise normal source, diagnostics, terminal selections, and diffs.
 */
export function redactContextSecrets(input: string): string {
  let output = input;
  output = output.replace(PRIVATE_KEY_BLOCK, REDACTED);
  output = output.replace(URI_CREDENTIALS, "$1<redacted>@");
  output = output.replace(AUTHORIZATION_HEADER_LINE, `$1$2${REDACTED}`);
  output = output.replace(AUTHORIZATION_VALUE, `$1${REDACTED}`);
  output = output.replace(FLAG_SECRET, `$1${REDACTED}`);
  output = output.replace(XML_SECRET, `$1${REDACTED}$2`);
  output = output.replace(JSON_SECRET, `$1${REDACTED}`);
  output = output.replace(ASSIGNMENT_SECRET, `$1${REDACTED}`);
  output = output.replace(ENV_SECRET, `$1$2${REDACTED}`);
  output = output.replace(KNOWN_TOKEN, REDACTED);
  output = output.replace(JWT, REDACTED);
  output = output.replace(HIGH_ENTROPY_TOKEN, (token) => isLikelySecretToken(token) ? REDACTED : token);
  return output;
}

function isLikelySecretToken(token: string): boolean {
  // Ordinary long identifiers made from a single class are kept. Mixed-class,
  // high-entropy strings are redacted even when the provider prefix is unknown.
  const classes = [/[a-z]/.test(token), /[A-Z]/.test(token), /\d/.test(token), /[_-]/.test(token)]
    .filter(Boolean).length;
  if (classes < 3) {
    return false;
  }
  const counts = new Map<string, number>();
  for (const char of token) {
    counts.set(char, (counts.get(char) ?? 0) + 1);
  }
  let entropy = 0;
  for (const count of counts.values()) {
    const probability = count / token.length;
    entropy -= probability * Math.log2(probability);
  }
  return entropy >= 3.5;
}
