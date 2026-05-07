import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Components } from 'react-markdown';

interface MarkdownRendererProps {
  content: string;
}

/**
 * LLMs sometimes emit escape sequences as literal two-character strings
 * (e.g. backslash + "n") rather than real control characters.  Convert the
 * most common ones so that downstream markdown parsing works correctly.
 */
 function unescapeLiteralEscapes(text: string): string {
  return text
    .replace(/\\n/g, '\n')
    .replace(/\\r/g, '\r')
    .replace(/\\t/g, '\t');
}

/**
 * Normalizes markdown table content that may arrive with all rows on a single
 * line (a common LLM output quirk).  When the alignment row (`| :--- |`) is
 * detected inside a longer line, each `| … |` row segment is split onto its
 * own line so that remark-gfm can parse the table correctly.
 *
 * Row boundaries are identified by the pattern `| |` — a closing pipe of one
 * row followed only by whitespace and then the opening pipe of the next row.
 * Normal intra-row cell separators always have non-empty cell content between
 * the two pipes, so they are not matched.
 */
 function normalizeTableMarkdown(text: string): string {
  return text
    .split('\n')
    .map((line) => {
      // Only attempt to reformat lines that look like concatenated table rows:
      // they must start with `|` and contain a GFM alignment-row marker
      // (`:---`, `---`, `:--:`, etc.) to limit false positives.
      if (!line.startsWith('|') || !/\|[ \t]*:?-+:?[ \t]*\|/.test(line)) {
        return line;
      }

      // Row boundaries: `|` followed by ONE OR MORE whitespace chars followed
      // immediately by `|`.  Inside a row, each `|` is followed by a cell value
      // (non-`|` text), so intra-row separators never match this pattern.
      const parts = line.split(/\|[ \t]+\|/);
      if (parts.length <= 1) return line;

      // Re-attach the `|` delimiters that were consumed by the split.
      const rows = parts.map((part, i) => {
        const trimmed = part.trim();
        // First fragment already starts with `|`; later ones lost their leading `|`.
        const withLeading = i === 0 ? trimmed : `| ${trimmed}`;
        // All rows must end with `|`.
        return withLeading.endsWith('|') ? withLeading : `${withLeading} |`;
      });

      return rows.join('\n');
    })
    .join('\n');
}

const markdownComponents: Components = {
  // remark-gfm's autolink-literals feature turns bare URLs (e.g. https://example.com)
  // into <a> tags automatically.  In LLM output these URLs are usually meant as plain
  // text citations, not navigable links.  We detect autolinks by comparing the href to
  // the sole text child — when they match the user wrote a raw URL, not a Markdown link
  // like [label](url).  In that case we render the URL as an unstyled <span> so it
  // doesn't appear as a blue underlined hyperlink.  Explicit Markdown links are
  // preserved as normal <a> elements.
  a: ({ href, children }) => {
    const textContent = typeof children === 'string'
      ? children
      : Array.isArray(children) && children.length === 1 && typeof children[0] === 'string'
        ? children[0]
        : null;
    if (textContent !== null && textContent === href) {
      return <span>{textContent}</span>;
    }
    return (
      <a href={href} className="text-blue-600 hover:underline" target="_blank" rel="noreferrer">
        {children}
      </a>
    );
  },
};

const tableComponents: Components = {
  table: ({ children }) => (
    <div className="overflow-x-auto my-4">
      <table className="min-w-full border-collapse border border-gray-300 text-sm">
        {children}
      </table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="bg-gray-100">{children}</thead>
  ),
  tbody: ({ children }) => (
    <tbody className="divide-y divide-gray-200">{children}</tbody>
  ),
  tr: ({ children }) => (
    <tr className="even:bg-gray-50">{children}</tr>
  ),
  th: ({ children }) => (
    <th className="border border-gray-300 px-3 py-2 text-left font-semibold text-gray-700 whitespace-nowrap">
      {children}
    </th>
  ),
  td: ({ children }) => (
    <td className="border border-gray-300 px-3 py-2 text-gray-700 align-top">
      {children}
    </td>
  ),
};

export default function MarkdownRenderer({ content }: MarkdownRendererProps) {
  const normalizedContent = normalizeTableMarkdown(unescapeLiteralEscapes(content));
  return (
    <div className="markdown-content prose prose-sm max-w-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ ...markdownComponents, ...tableComponents }}>
        {normalizedContent}
      </ReactMarkdown>
    </div>
  );
}
