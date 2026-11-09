"use client";

import React, { useEffect, useState } from "react";

/**
 * Components showcase, ported verbatim from the design-system handoff reference
 * (feat/ui-redesign-design-system, commit 9db8051). Self-contained: inline styles
 * plus its own keyframes; the Foundations reference lives in foundations-ui.tsx.
 */
const KEYFRAMES = `
@keyframes bpPulse { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.55); opacity: 0.35; } }
@keyframes bpSpin { to { transform: rotate(360deg); } }
@keyframes bpFlow { to { stroke-dashoffset: -60; } }
@keyframes bpShimmer { 0% { transform: translateX(-120%); } 100% { transform: translateX(320%); } }
@keyframes bpToast { from { transform: translateY(18px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
@keyframes bpModal { from { transform: translateY(14px) scale(0.99); opacity: 0; } to { transform: translateY(0) scale(1); opacity: 1; } }
@keyframes bpBlink { 0%,49% { opacity: 1; } 50%,100% { opacity: 0; } }
@keyframes bpBreathe { 0%,100% { opacity: 0.5; } 50% { opacity: 1; } }
`;

function Keyframes() {
  return <style>{KEYFRAMES}</style>;
}

const orbitMask = "radial-gradient(circle, transparent 52%, #000 54%)";
const orbitMask50 = "radial-gradient(circle, transparent 50%, #000 52%)";
const orbitMask58 = "radial-gradient(circle, transparent 58%, #000 60%)";
const conic = "conic-gradient(from 0deg,#2ec5c5,#2f6fe0,transparent 72%)";

/* ============ COMPONENTS ============ */
export function ComponentsScreen() {
  const [toggle1, setToggle1] = useState(true);
  const [toggle2, setToggle2] = useState(false);
  const [checkA, setCheckA] = useState(true);
  const [checkB, setCheckB] = useState(true);
  const [modalOpen, setModalOpen] = useState(false);
  const [toastVisible, setToastVisible] = useState(false);
  const [composerText, setComposerText] = useState("Ship the billing redesign to production");

  useEffect(() => {
    if (!toastVisible) return;
    const t = setTimeout(() => setToastVisible(false), 3000);
    return () => clearTimeout(t);
  }, [toastVisible]);

  const trackStyle = (on: boolean): React.CSSProperties => ({
    position: "relative",
    width: "44px",
    height: "24px",
    flexShrink: 0,
    borderRadius: "999px",
    border: "none",
    cursor: "pointer",
    padding: 0,
    background: on ? "linear-gradient(120deg,#2ec5c5,#2f6fe0)" : "#1c2740",
    transition: "background 0.18s ease",
  });
  const knobStyle = (on: boolean): React.CSSProperties => ({
    position: "absolute",
    top: "3px",
    left: "3px",
    width: "18px",
    height: "18px",
    borderRadius: "50%",
    background: "#fff",
    transform: on ? "translateX(20px)" : "translateX(0)",
    transition: "transform 0.18s ease",
  });
  const boxStyle = (on: boolean): React.CSSProperties => ({
    width: "20px",
    height: "20px",
    borderRadius: "5px",
    flexShrink: 0,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: on ? "linear-gradient(120deg,#2ec5c5,#2f6fe0)" : "transparent",
    border: on ? "1px solid transparent" : "1px solid #26365c",
  });
  const tickStyle = (on: boolean): React.CSSProperties => ({ opacity: on ? 1 : 0 });

  return (
    <section data-screen-label="Components">
      <Keyframes />
      <div style={{ padding: "44px 58px 32px", borderBottom: "1px solid var(--boss-c-16213c)" }}>
        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-2ec5c5)", marginBottom: "13px" }}>Component Library</div>
        <h1 style={{ fontFamily: "'Sora',sans-serif", fontWeight: 800, fontSize: "36px", margin: 0, letterSpacing: "-0.02em" }}>Components</h1>
      </div>

      {/* Buttons */}
      <div style={{ padding: "38px 58px", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Buttons</div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: "14px", alignItems: "center" }}>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 700, color: "var(--boss-c-06121f)", padding: "11px 20px", borderRadius: "8px", border: "none", background: "linear-gradient(120deg,var(--boss-c-2ec5c5),var(--boss-c-2f6fe0))", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "8px" }}>Assign Outcome <span style={{ fontSize: "15px" }}>{"→"}</span></button>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-fff)", padding: "11px 20px", borderRadius: "8px", border: "1px solid var(--boss-c-3a6de0)", background: "var(--boss-c-a0cc83311e)", cursor: "pointer" }}>Primary</button>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-c3d0e8)", padding: "11px 20px", borderRadius: "8px", border: "1px solid var(--boss-c-26365c)", background: "transparent", cursor: "pointer" }}>Secondary</button>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-7fffe0)", padding: "11px 20px", borderRadius: "8px", border: "1px solid var(--boss-c-8c8784fa36)", background: "transparent", cursor: "pointer" }}>Approve</button>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-ff9fb0)", padding: "11px 20px", borderRadius: "8px", border: "1px solid var(--boss-c-b36e9ece81)", background: "transparent", cursor: "pointer" }}>Halt Agent</button>
          <button style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-8ea3c8)", padding: "11px 20px", borderRadius: "8px", border: "1px solid transparent", background: "transparent", cursor: "pointer" }}>Ghost</button>
        </div>
      </div>

      {/* Composer */}
      <div style={{ padding: "38px 58px", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Outcome Composer</div>
        <div style={{ maxWidth: "720px", border: "1px solid var(--boss-c-26365c)", borderRadius: "10px", background: "var(--boss-c-0e1730)", overflow: "hidden" }}>
          <div style={{ padding: "18px 20px 8px", display: "flex", alignItems: "center", gap: "10px" }}><span style={{ width: "15px", height: "15px", borderRadius: "50%", background: conic, WebkitMask: orbitMask, mask: orbitMask, animation: "bpSpin 3s linear infinite" }} /><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-2ec5c5)" }}>New outcome</span></div>
          <div style={{ padding: "4px 20px 16px", fontFamily: "'Sora',sans-serif", fontSize: "19px", fontWeight: 500, color: "var(--boss-c-eef3fb)", lineHeight: 1.4 }}>{composerText}<span style={{ display: "inline-block", width: "2px", height: "20px", background: "var(--boss-c-2ec5c5)", marginLeft: "2px", verticalAlign: "-3px", animation: "bpBlink 1.1s step-end infinite" }} /></div>
          <div style={{ display: "flex", alignItems: "center", gap: "16px", padding: "0 20px 16px", flexWrap: "wrap" }}>
            <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", color: "var(--boss-c-64769a)" }}>try</span>
            <button onClick={() => setComposerText("Ship the billing redesign")} style={{ fontFamily: "inherit", fontSize: "13px", color: "var(--boss-c-9fb0ce)", padding: 0, border: "none", background: "none", cursor: "pointer", borderBottom: "1px solid transparent" }}>Ship the billing redesign</button>
            <button onClick={() => setComposerText("Cut infra cost 20%")} style={{ fontFamily: "inherit", fontSize: "13px", color: "var(--boss-c-9fb0ce)", padding: 0, border: "none", background: "none", cursor: "pointer", borderBottom: "1px solid transparent" }}>Cut infra cost 20%</button>
            <button onClick={() => setComposerText("Launch on Product Hunt")} style={{ fontFamily: "inherit", fontSize: "13px", color: "var(--boss-c-9fb0ce)", padding: 0, border: "none", background: "none", cursor: "pointer", borderBottom: "1px solid transparent" }}>Launch on Product Hunt</button>
          </div>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "14px 20px", borderTop: "1px solid var(--boss-c-1c2740)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "9px" }}><span style={{ width: "14px", height: "14px", borderRadius: "50%", background: conic, WebkitMask: orbitMask50, mask: orbitMask50, animation: "bpSpin 3s linear infinite" }} /><span style={{ fontSize: "13px", fontWeight: 600, color: "var(--boss-c-2ec5c5)" }}>Auto-route</span><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", color: "var(--boss-c-5e719a)" }}>{"→"} 3 agents</span></div>
            <button style={{ fontFamily: "inherit", fontSize: "13.5px", fontWeight: 700, color: "var(--boss-c-06121f)", padding: "9px 18px", borderRadius: "8px", border: "none", background: "linear-gradient(120deg,var(--boss-c-2ec5c5),var(--boss-c-2f6fe0))", cursor: "pointer", display: "inline-flex", alignItems: "center", gap: "7px" }}>Assign <span>{"→"}</span></button>
          </div>
        </div>
      </div>

      {/* Agent state + orbs + progress */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ padding: "38px 44px 40px 58px", borderRight: "1px solid var(--boss-c-14203a)" }}>
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Agent State</div>
          <div style={{ display: "flex", flexDirection: "column", gap: "14px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ position: "relative", width: "8px", height: "8px", display: "inline-flex" }}><span style={{ position: "absolute", inset: 0, borderRadius: "50%", background: "var(--boss-c-2ec5c5)", animation: "bpPulse 2s ease-out infinite" }} /><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-2ec5c5)" }} /></span><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 700, letterSpacing: "0", color: "var(--boss-c-2ec5c5)", width: "96px" }}>Running</span><span style={{ fontSize: "13px", color: "var(--boss-c-8ea3c8)" }}>actively working</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-34d399)" }} /><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 700, letterSpacing: "0", color: "var(--boss-c-34d399)", width: "96px" }}>Verified</span><span style={{ fontSize: "13px", color: "var(--boss-c-8ea3c8)" }}>done, receipt written</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-f5b544)" }} /><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 700, letterSpacing: "0", color: "var(--boss-c-f5b544)", width: "96px" }}>Awaiting</span><span style={{ fontSize: "13px", color: "var(--boss-c-8ea3c8)" }}>needs your decision</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-f2647a)", animation: "bpBreathe 1.4s ease-in-out infinite" }} /><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 700, letterSpacing: "0", color: "var(--boss-c-f2647a)", width: "96px" }}>Blocked</span><span style={{ fontSize: "13px", color: "var(--boss-c-8ea3c8)" }}>can&apos;t proceed</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-64769a)" }} /><span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 700, letterSpacing: "0", color: "var(--boss-c-8ea3c8)", width: "96px" }}>Idle</span><span style={{ fontSize: "13px", color: "var(--boss-c-8ea3c8)" }}>no active outcome</span></div>
          </div>
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", margin: "32px 0 18px" }}>Progress</div>
          <div style={{ display: "flex", alignItems: "center", gap: "26px" }}>
            <div style={{ position: "relative", width: "60px", height: "60px" }}><svg width="60" height="60" style={{ transform: "rotate(-90deg)" }} aria-hidden="true"><circle cx="30" cy="30" r="25" fill="none" stroke="#1c2740" strokeWidth="5" /><circle cx="30" cy="30" r="25" fill="none" stroke="#2ec5c5" strokeWidth="5" strokeLinecap="round" strokeDasharray="157" strokeDashoffset="55" /></svg><span style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", fontWeight: 600, color: "var(--boss-c-eef3fb)" }}>65%</span></div>
            <div style={{ flex: 1 }}><div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", marginBottom: "7px" }}><span style={{ color: "var(--boss-c-9fb0ce)", fontWeight: 600 }}>Billing redesign</span><span style={{ fontFamily: "'JetBrains Mono',monospace", color: "var(--boss-c-64769a)" }}>7/11 tasks</span></div><div style={{ height: "6px", background: "var(--boss-c-141f36)", overflow: "hidden", position: "relative" }}><div style={{ position: "absolute", inset: 0, width: "64%", background: "linear-gradient(90deg,var(--boss-c-2ec5c5),var(--boss-c-2f6fe0))" }} /><div style={{ position: "absolute", top: 0, bottom: 0, left: 0, width: "40%", background: "linear-gradient(90deg,transparent,var(--boss-c-43bcc0762e),transparent)", animation: "bpShimmer 2.2s ease-in-out infinite" }} /></div></div>
          </div>
        </div>
        <div style={{ padding: "38px 58px 40px 44px" }}>
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Agent Orbs</div>
          <div style={{ display: "flex", gap: "30px", flexWrap: "wrap" }}>
            <div style={{ textAlign: "center" }}><div style={{ position: "relative", width: "52px", height: "52px", margin: "0 auto" }}><div style={{ position: "absolute", inset: "-3px", borderRadius: "50%", background: conic, WebkitMask: orbitMask58, mask: orbitMask58, animation: "bpSpin 3s linear infinite" }} /><div style={{ width: "52px", height: "52px", borderRadius: "50%", background: "linear-gradient(140deg,var(--boss-c-1e3a63),var(--boss-c-122845))", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "15px", color: "var(--boss-c-dce8fb)" }}>PM</div></div><div style={{ fontSize: "11px", color: "var(--boss-c-8ea3c8)", marginTop: "9px" }}>running</div></div>
            <div style={{ textAlign: "center" }}><div style={{ position: "relative", width: "52px", height: "52px", margin: "0 auto" }}><div style={{ position: "absolute", inset: "-3px", borderRadius: "50%", border: "2px solid var(--boss-c-34d399)" }} /><div style={{ width: "52px", height: "52px", borderRadius: "50%", background: "linear-gradient(140deg,var(--boss-c-1e3a63),var(--boss-c-122845))", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "15px", color: "var(--boss-c-dce8fb)" }}>ARC</div><span style={{ position: "absolute", right: "-2px", bottom: "-2px", width: "16px", height: "16px", borderRadius: "50%", background: "var(--boss-c-34d399)", border: "2px solid var(--boss-c-0c1424)", display: "flex", alignItems: "center", justifyContent: "center" }}><svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="#06121f" strokeWidth="4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-11" /></svg></span></div><div style={{ fontSize: "11px", color: "var(--boss-c-8ea3c8)", marginTop: "9px" }}>verified</div></div>
            <div style={{ textAlign: "center" }}><div style={{ position: "relative", width: "52px", height: "52px", margin: "0 auto" }}><div style={{ position: "absolute", inset: "-3px", borderRadius: "50%", border: "2px solid var(--boss-c-f2647a)", animation: "bpBreathe 1.4s ease-in-out infinite" }} /><div style={{ width: "52px", height: "52px", borderRadius: "50%", background: "linear-gradient(140deg,var(--boss-c-3a2436),var(--boss-c-241624))", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "15px", color: "var(--boss-c-f0c6ce)" }}>QA</div><span style={{ position: "absolute", right: "-2px", bottom: "-2px", width: "16px", height: "16px", borderRadius: "50%", background: "var(--boss-c-f2647a)", border: "2px solid var(--boss-c-0c1424)", color: "var(--boss-c-fff)", fontSize: "11px", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800 }}>!</span></div><div style={{ fontSize: "11px", color: "var(--boss-c-8ea3c8)", marginTop: "9px" }}>blocked</div></div>
            <div style={{ textAlign: "center" }}><div style={{ position: "relative", width: "52px", height: "52px", margin: "0 auto" }}><div style={{ position: "absolute", inset: "-3px", borderRadius: "50%", border: "1px dashed var(--boss-c-2b3c63)" }} /><div style={{ width: "52px", height: "52px", borderRadius: "50%", background: "var(--boss-c-141f36)", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "15px", color: "var(--boss-c-64769a)" }}>CMO</div></div><div style={{ fontSize: "11px", color: "var(--boss-c-8ea3c8)", marginTop: "9px" }}>idle</div></div>
          </div>
        </div>
      </div>

      {/* Controls + overlays */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ padding: "38px 44px 40px 58px", borderRight: "1px solid var(--boss-c-14203a)" }}>
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Controls</div>
          <div style={{ maxWidth: "360px", display: "flex", flexDirection: "column" }}>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid var(--boss-c-14203a)" }}><span style={{ fontSize: "14px", color: "var(--boss-c-c3d0e8)" }}>Require approval for merges</span><button onClick={() => setToggle1((v) => !v)} style={trackStyle(toggle1)}><span style={knobStyle(toggle1)} /></button></div>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 0", borderBottom: "1px solid var(--boss-c-14203a)" }}><span style={{ fontSize: "14px", color: "var(--boss-c-c3d0e8)" }}>Let agents spawn sub-agents</span><button onClick={() => setToggle2((v) => !v)} style={trackStyle(toggle2)}><span style={knobStyle(toggle2)} /></button></div>
            <button onClick={() => setCheckA((v) => !v)} style={{ fontFamily: "inherit", display: "flex", alignItems: "center", gap: "12px", background: "none", border: "none", padding: "13px 0", cursor: "pointer", color: "var(--boss-c-c3d0e8)", fontSize: "14px", borderBottom: "1px solid var(--boss-c-14203a)" }}><span style={boxStyle(checkA)}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#06121f" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" style={tickStyle(checkA)} aria-hidden="true"><path d="M5 12l5 5 9-11" /></svg></span>Attach receipts to every action</button>
            <button onClick={() => setCheckB((v) => !v)} style={{ fontFamily: "inherit", display: "flex", alignItems: "center", gap: "12px", background: "none", border: "none", padding: "13px 0", cursor: "pointer", color: "var(--boss-c-c3d0e8)", fontSize: "14px" }}><span style={boxStyle(checkB)}><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#06121f" strokeWidth="3.4" strokeLinecap="round" strokeLinejoin="round" style={tickStyle(checkB)} aria-hidden="true"><path d="M5 12l5 5 9-11" /></svg></span>Notify me on blocked work</button>
          </div>
        </div>
        <div style={{ padding: "38px 58px 40px 44px" }}>
          <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "18px" }}>Overlays</div>
          <div style={{ display: "flex", gap: "14px" }}><button onClick={() => setModalOpen(true)} data-testid="ds-open-modal" style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-fff)", padding: "10px 18px", borderRadius: "8px", border: "1px solid var(--boss-c-3a6de0)", background: "var(--boss-c-a0cc83311e)", cursor: "pointer" }}>Open Modal</button><button onClick={() => setToastVisible(true)} style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-c3d0e8)", padding: "10px 18px", borderRadius: "8px", border: "1px solid var(--boss-c-26365c)", background: "transparent", cursor: "pointer" }}>Fire Toast</button></div>
          <p style={{ fontSize: "13px", color: "var(--boss-c-64769a)", margin: "16px 0 0", lineHeight: 1.6, maxWidth: "340px" }}>Modals confirm irreversible operator actions. Toasts confirm completed agent work.</p>
        </div>
      </div>

      {/* MODAL */}
      {modalOpen && (
        <div onClick={() => setModalOpen(false)} style={{ position: "fixed", inset: 0, zIndex: "var(--z-page)", background: "var(--boss-c-499adfdc30)", backdropFilter: "blur(5px)", display: "flex", alignItems: "center", justifyContent: "center", padding: "24px" }}>
          <div onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true" data-testid="ds-modal" style={{ width: "440px", maxWidth: "100%", borderRadius: "12px", overflow: "hidden", border: "1px solid var(--boss-c-2b3c63)", background: "var(--boss-c-0e1730)", boxShadow: "0 40px 100px var(--boss-c-e946900b5d)", animation: "bpModal 0.22s ease" }}>
            <div style={{ padding: "22px 24px 6px" }}><div style={{ display: "flex", alignItems: "center", gap: "11px" }}><span style={{ width: "38px", height: "38px", borderRadius: "9px", background: "var(--boss-c-19b2c71132)", border: "1px solid var(--boss-c-226204e3fe)", display: "flex", alignItems: "center", justifyContent: "center" }}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#f2647a" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M12 9v4M12 17h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /></svg></span><span style={{ fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "17px" }}>Halt this agent?</span></div></div>
            <div style={{ padding: "12px 24px 20px", fontSize: "14px", lineHeight: 1.6, color: "var(--boss-c-9fb0ce)" }}>Halting <span style={{ fontFamily: "'JetBrains Mono',monospace", color: "var(--boss-c-2ec5c5)" }}>Backend Engineer</span> stops its current task immediately. In-flight work is checkpointed and a receipt is written. You stay in control.</div>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: "11px", padding: "0 24px 22px" }}><button onClick={() => setModalOpen(false)} style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 600, color: "var(--boss-c-c3d0e8)", padding: "10px 18px", borderRadius: "8px", border: "1px solid var(--boss-c-26365c)", background: "transparent", cursor: "pointer" }}>Cancel</button><button onClick={() => setModalOpen(false)} style={{ fontFamily: "inherit", fontSize: "14px", fontWeight: 700, color: "var(--boss-c-06121f)", padding: "10px 18px", borderRadius: "8px", border: "none", background: "var(--boss-c-f2647a)", cursor: "pointer" }}>Halt agent</button></div>
          </div>
        </div>
      )}

      {/* TOAST */}
      {toastVisible && (
        <div style={{ position: "fixed", bottom: "26px", right: "26px", zIndex: "var(--z-toast)", display: "flex", alignItems: "center", gap: "13px", padding: "14px 18px", borderRadius: "10px", border: "1px solid var(--boss-c-4f67c7d805)", background: "var(--boss-c-0f2436)", boxShadow: "0 24px 60px var(--boss-c-f91928c08e)", animation: "bpToast 0.28s ease" }}>
          <span style={{ width: "30px", height: "30px", borderRadius: "50%", background: "var(--boss-c-2d552d73c6)", display: "flex", alignItems: "center", justifyContent: "center" }}><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#34d399" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M5 12l5 5 9-11" /></svg></span>
          <div><div style={{ fontSize: "14px", fontWeight: 700, color: "var(--boss-c-eef3fb)" }}>Outcome verified</div><div style={{ fontSize: "12.5px", color: "var(--boss-c-8fb8b0)", marginTop: "2px" }}>Architect signed off, receipt written.</div></div>
        </div>
      )}
    </section>
  );
}

/* ============ FOUNDATIONS ============ */
export function FoundationsScreen() {
  return (
    <section data-screen-label="Foundations">
      <Keyframes />
      <div style={{ position: "relative", padding: "60px 58px 50px", overflow: "hidden", borderBottom: "1px solid var(--boss-c-16213c)" }}>
        <div style={{ position: "absolute", inset: 0, background: "radial-gradient(520px 320px at 88% 15%, var(--boss-c-721571823e), transparent 60%)", pointerEvents: "none" }} />
        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-2ec5c5)", marginBottom: "20px" }}>BOSS / Design System</div>
        <h1 style={{ fontFamily: "'Sora',sans-serif", fontWeight: 800, fontSize: "52px", lineHeight: 1.03, margin: 0, maxWidth: "780px", letterSpacing: "-0.02em" }}>
          Run a company of <span style={{ background: "linear-gradient(120deg,var(--boss-c-2ec5c5),var(--boss-c-4a90ff))", WebkitBackgroundClip: "text", backgroundClip: "text", color: "transparent" }}>AI workers.</span>
          <br />Stay in command.
        </h1>
        <p style={{ maxWidth: "600px", margin: "22px 0 0", fontSize: "16.5px", lineHeight: 1.6, color: "var(--boss-c-9fb0ce)" }}>The visual language for BOSS, an operating system for AI labor. Flat surfaces, hairline structure, accountable agents. Powerful without being loud; the operator is always in control.</p>
      </div>

      {/* Colors */}
      <div style={{ padding: "48px 58px 42px", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: "15px", marginBottom: "26px" }}>
          <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-5e719a)" }}>01</span>
          <h2 style={{ fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "24px", margin: 0, letterSpacing: "-0.01em" }}>Color</h2>
        </div>

        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "13px" }}>Surfaces</div>
        <div style={{ display: "flex", border: "1px solid var(--boss-c-1c2740)", marginBottom: "30px" }}>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-090d17)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Void</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#090D17</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-0c1424)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Base</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#0C1424</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-101a30)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Panel</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#101A30</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-16213c)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Raised</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#16213C</div></div></div>
          <div style={{ flex: 1 }}><div style={{ height: "64px", background: "linear-gradient(135deg,var(--boss-c-2ec5c5),var(--boss-c-2f6fe0) 55%,var(--boss-c-1b3a6b))" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Brand</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>teal {"→"} navy</div></div></div>
        </div>

        <div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-64769a)", marginBottom: "13px" }}>Signal, agent state</div>
        <div style={{ display: "flex", border: "1px solid var(--boss-c-1c2740)" }}>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-2ec5c5)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Running</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#2EC5C5</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-34d399)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Verified</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#34D399</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-f5b544)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Awaiting</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#F5B544</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-f2647a)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Blocked</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#F2647A</div></div></div>
          <div style={{ flex: 1, borderRight: "1px solid var(--boss-c-1c2740)" }}><div style={{ height: "64px", background: "var(--boss-c-2f6fe0)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Primary</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#2F6FE0</div></div></div>
          <div style={{ flex: 1 }}><div style={{ height: "64px", background: "var(--boss-c-64769a)" }} /><div style={{ padding: "10px 13px", borderTop: "1px solid var(--boss-c-1c2740)" }}><div style={{ fontSize: "12.5px", fontWeight: 600 }}>Idle</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "2px" }}>#64769A</div></div></div>
        </div>
      </div>

      {/* Type + Motion legend */}
      <div style={{ display: "grid", gridTemplateColumns: "1.3fr 1fr", borderBottom: "1px solid var(--boss-c-14203a)" }}>
        <div style={{ padding: "44px 44px 48px 58px", borderRight: "1px solid var(--boss-c-14203a)" }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: "15px", marginBottom: "22px" }}>
            <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-5e719a)" }}>02</span>
            <h2 style={{ fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "24px", margin: 0, letterSpacing: "-0.01em" }}>Typography</h2>
          </div>
          <div style={{ border: "1px solid var(--boss-c-1c2740)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "24px", padding: "20px 22px", borderBottom: "1px solid var(--boss-c-14203a)" }}><div style={{ width: "140px", flexShrink: 0 }}><div style={{ fontSize: "13.5px", fontWeight: 700 }}>Sora</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "3px" }}>Display</div></div><div style={{ fontFamily: "'Sora',sans-serif", fontWeight: 800, fontSize: "28px", letterSpacing: "-0.02em" }}>Assign the outcome</div></div>
            <div style={{ display: "flex", alignItems: "center", gap: "24px", padding: "20px 22px", borderBottom: "1px solid var(--boss-c-14203a)" }}><div style={{ width: "140px", flexShrink: 0 }}><div style={{ fontSize: "13.5px", fontWeight: 700 }}>Manrope</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "3px" }}>Interface</div></div><div style={{ fontSize: "17px", color: "var(--boss-c-c3d0e8)" }}>The architect drafted a schema and routed it to QA.</div></div>
            <div style={{ display: "flex", alignItems: "center", gap: "24px", padding: "20px 22px" }}><div style={{ width: "140px", flexShrink: 0 }}><div style={{ fontSize: "13.5px", fontWeight: 700 }}>JetBrains Mono</div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "10.5px", color: "var(--boss-c-64769a)", marginTop: "3px" }}>Data, Receipts</div></div><div style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "15px", color: "var(--boss-c-2ec5c5)" }}>rcpt_8f2a, +142 −18, 0.9s</div></div>
          </div>
        </div>
        <div style={{ padding: "44px 58px 48px 44px" }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: "15px", marginBottom: "22px" }}>
            <span style={{ fontFamily: "'JetBrains Mono',monospace", fontSize: "11px", letterSpacing: "0", textTransform: "none", color: "var(--boss-c-5e719a)" }}>03</span>
            <h2 style={{ fontFamily: "'Sora',sans-serif", fontWeight: 700, fontSize: "24px", margin: 0, letterSpacing: "-0.01em" }}>Motion = liveness</h2>
          </div>
          <p style={{ fontSize: "14px", color: "var(--boss-c-9fb0ce)", margin: "0 0 20px", lineHeight: 1.6 }}>Motion signals that agents are <em style={{ color: "var(--boss-c-c3d0e8)", fontStyle: "normal", fontWeight: 600 }}>alive and accountable</em>. Running work breathes; nothing decorative moves.</p>
          <div style={{ display: "flex", flexDirection: "column", gap: "16px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "14px" }}><span style={{ position: "relative", width: "12px", height: "12px", display: "inline-flex", alignItems: "center", justifyContent: "center" }}><span style={{ position: "absolute", width: "12px", height: "12px", borderRadius: "50%", background: "var(--boss-c-2ec5c5)", animation: "bpPulse 2s ease-out infinite" }} /><span style={{ width: "8px", height: "8px", borderRadius: "50%", background: "var(--boss-c-2ec5c5)" }} /></span><span style={{ fontSize: "14px", color: "var(--boss-c-c3d0e8)" }}>Pulse, active / running</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "14px" }}><span style={{ width: "16px", height: "16px", borderRadius: "50%", background: conic, WebkitMask: orbitMask, mask: orbitMask, animation: "bpSpin 2.6s linear infinite" }} /><span style={{ fontSize: "14px", color: "var(--boss-c-c3d0e8)" }}>Orbit ring, agent working</span></div>
            <div style={{ display: "flex", alignItems: "center", gap: "14px" }}><svg width="52" height="16" aria-hidden="true"><line x1="2" y1="8" x2="50" y2="8" stroke="#2f6fe0" strokeWidth="2" strokeDasharray="5 5" style={{ animation: "bpFlow 1s linear infinite" }} /></svg><span style={{ fontSize: "14px", color: "var(--boss-c-c3d0e8)" }}>Flow, handoff between agents</span></div>
          </div>
        </div>
      </div>
    </section>
  );
}

