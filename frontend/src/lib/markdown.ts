// Line-level markdown structure: fences and rules are dropped, the rest lose their markers.
const fence = /^\s*(?:```|~~~)/;
const thematicBreak = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const blockquote = /^\s*(?:>\s?)+/;
const heading = /^\s*#{1,6}(?=\s|$)/;
// Ordered markers capped at 3 digits, as in the editor, so "1999. It began" keeps its year.
const listItem = /^\s*(?:[-*+]|\d{1,3}[.)])\s+(?:\[[ xX]\]\s+)?/;

/**
 * The text of markdown written in the MarkdownEditor, on one line: what a search query means
 * without the formatting the toolbar, shortcuts and list continuation add. A list marker would
 * otherwise reach the keyword search as an operator ("- apples" excludes apples). Search syntax
 * typed on purpose is kept: "quoted phrases", -exclusions (no space after the dash) and OR.
 */
export function stripMarkdown(text: string): string {
  return (
    text
      .split("\n")
      .filter((line) => !fence.test(line) && !thematicBreak.test(line))
      .map((line) => line.replace(blockquote, "").replace(heading, "").replace(listItem, ""))
      .join(" ")
      // Links and images keep their text.
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/`+|\*+|~~/g, "")
      // Underscores only at a word's edge, so snake_case survives but _emphasis_ doesn't.
      .replace(/(?<![\p{L}\p{N}])_+|_+(?![\p{L}\p{N}])/gu, "")
      .replace(/\s+/g, " ")
      .trim()
  );
}
