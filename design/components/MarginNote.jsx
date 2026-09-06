export function NoteWrap({ children }) {
  return <div className="note-wrap">{children}</div>;
}

export default function MarginNote({ children }) {
  return <aside className="margin-note">{children}</aside>;
}
