# RootPilot

RootPilot is an evidence-driven AI investigation system designed to improve debugging quality through evidence analysis, hypothesis validation, engineer feedback, and investigation review.

---

# Why RootPilot?

RootPilot started as a tool to automate part of the debugging workflow.

```text
Issue
    ↓
Collect evidence
    ↓
Generate hypotheses
    ↓
Engineer feedback
    ↓
Refine investigation
    ↓
Propose a fix
```

As the project evolved, RootPilot began exploring how to find root causes in large engineering environments.

This led to research areas such as:

- [Routing RAG](https://ziyingchen-dev.github.io/EngineeringWiki/ai/rootpilot/routing-rag/)
- [Multi-Domain Expert Routing](https://ziyingchen-dev.github.io/EngineeringWiki/ai/rootpilot/multi-domain-expert-routing/)
- [Repository RAG](https://ziyingchen-dev.github.io/EngineeringWiki/ai/rootpilot/repository-rag/)

The original assumption was that locating the right repository, module, and source files would be the hardest part of debugging large codebases.

The goal was to narrow the search space before investigation begins.

```text
Issue
    ↓
Routing RAG
    ↓
Repository / Module Selection
    ↓
Repository RAG
    ↓
Investigation
```

However, implementation revealed an unexpected result.

For smaller repositories, modern LLMs can often inspect most or all of the source code directly. Repository discovery is not always the primary bottleneck.

Finding code is not always the hardest problem.

Understanding it correctly is.

As a result, RootPilot's focus has gradually expanded from code discovery to investigation quality.

Today, RootPilot is an evidence-driven AI investigation system designed to improve debugging quality.

---

# Investigation Challenges

The UefiNexus investigation uncovered a different class of problems.

- **Local Trap**: After identifying a suspected issue, the investigation keeps focusing on that area and building evidence around the same assumption.

- **Design History Loss**: A design decision exists, but the reason behind it is no longer visible in the available code, tests, messages, documentation, or investigation context.

- **Context Contamination**: Later investigations or reviews inherit assumptions and conclusions from earlier investigations instead of evaluating the evidence independently.

- **Confidence Inflation**: Confidence increases even though little or no new evidence has been discovered.

An investigation may find the correct repository, the correct file, and even the correct evidence, but still reach the wrong conclusion.

(Reference: [UefiNexus Invalid-Goto Investigation](https://ziyingchen-dev.github.io/EngineeringWiki/ai/rootpilot/uefinexus-invalid-goto-investigation/index.html))

---

# Project Status

RootPilot is still evolving.

Recent investigation experiments uncovered problems that were not part of the original design assumptions. As a result, the future direction of the project is still being explored.

Current implementations, workflows, and research directions should be considered experimental and may change significantly as new investigation challenges are discovered.

One possible future direction is to explore multiple independent investigation sessions:

```text
A: Investigate

B: Investigate

C: Review and challenge A and B
```

The goal is not simply to increase automation, but to improve investigation quality and reduce investigation failures such as local traps, design-history loss, context contamination, and confidence inflation.

---

# License

MIT. See [LICENSE](LICENSE).