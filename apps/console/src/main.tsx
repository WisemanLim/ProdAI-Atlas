import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { initAuth } from "./lib/auth";
import "./styles.css";

// 인증 모드(oidc)면 로그인·토큰 부착을 먼저 끝낸 뒤 렌더 (FR-PLT-IAM-01)
initAuth().finally(() =>
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <App />
    </StrictMode>,
  ),
);
