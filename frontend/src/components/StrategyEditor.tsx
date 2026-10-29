/**
 * Monaco Editor 策略代码编辑器.
 * 使用 @monaco-editor/react 封装 VS Code 引擎.
 */
import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor as MonacoEditor } from "monaco-editor";

interface Props {
  value: string;
  onChange: (code: string) => void;
  height?: number | string;
  readOnly?: boolean;
  onMount?: (editor: MonacoEditor.IStandaloneCodeEditor) => void;
}

export default function StrategyEditor({
  value,
  onChange,
  height = "100%",
  readOnly = false,
  onMount,
}: Props) {
  const handleMount: OnMount = (editor) => {
    onMount?.(editor);
  };

  return (
    <Editor
      height={height}
      defaultLanguage="python"
      theme="vs-dark"
      value={value}
      onChange={(v) => onChange(v ?? "")}
      onMount={handleMount}
      options={{
        minimap: { enabled: false },
        fontSize: 13,
        lineNumbers: "on",
        wordWrap: "on",
        scrollBeyondLastLine: false,
        readOnly,
        automaticLayout: true,
        padding: { top: 12, bottom: 12 },
        suggest: { showKeywords: true },
        lineHeight: 20,
        fontFamily: "'JetBrains Mono', 'Fira Code', Consolas, monospace",
      }}
    />
  );
}
