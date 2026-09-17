# Roadmap do Plane MCP Karval

Status: roadmap versionado para a linha `v0.3.4` e as próximas evoluções.

A tag `v0.3.3` permanece imutável. O controle de planejamento, issues,
review e release deste repositório é o GitHub; o projeto Hermes no Plane não é
usado como tracker deste MCP.

## Estado da v0.3.4

A `v0.3.4` amplia o contrato comum sem criar uma nova camada de plugins ou um
broker. O objetivo é aumentar cobertura documentada mantendo o dispatcher,
ledger, idempotência, tentativa única, readback e reconciliação existentes.

Entregas:

- cinco operações de Inbox Issues;
- três operações de relações entre work items;
- 233 ações documentadas, 231 operações HTTP e 139 mutações governadas;
- shape de resposta `relation_map` com as oito categorias oficiais;
- readback de presença/ausência para relações;
- readback de detalhe para atualização de Inbox Issue;
- verificador de paridade do contrato;
- gate de consistência de pacote, skill, registry e release notes;
- diagnóstico offline sem chamadas ao provider;
- testes de matriz para cada operação e mutação.

Fora do escopo: upload de anexos, plugins por harness e broker compartilhado.

## v0.4.x — capacidades que exigem fronteira nova

### v0.4.0 — anexos seguros e capability profiles

A primeira versão 0.4 deve tratar upload como uma capacidade dedicada, não como
mais uma chamada genérica de `plane_mutation_action`.

Entregas propostas:

1. `plane_upload_issue_attachment` com contrato próprio.
2. Allowlist de MIME, limite de tamanho e limite de tempo.
3. Entrada por arquivo temporário controlado ou bytes explicitamente limitados;
   nunca aceitar URL arbitrária do modelo como fonte implícita.
4. Proteção contra path traversal, symlink e exposição do conteúdo no ledger.
5. Fluxo de credencial temporária, upload, complete e readback do attachment.
6. Reconciliador de upload ambíguo sem replay automático.
7. Testes fake para sucesso, timeout, upload parcial, objeto órfão e rejeição.
8. Capability profiles explícitos:
   - `read_only`;
   - `standard_mutation`;
   - `lifecycle_operator`;
   - `attachments`;
   - `destructive`.
9. Catálogo que informa capacidades disponíveis sem expor credenciais ou
   conceder autorização adicional ao modelo.

Não incluir em 0.4.0: marketplace de plugins, download remoto, sandbox
presumido ou mudança automática de capability profile.

### v0.4.x — plugins por harness, em ondas separadas

Depois do contrato de anexos, implementar plugins somente quando houver regra
específica comprovada para um harness. A ordem recomendada é:

- P1: manifesto/I/O/hooks versionados;
- P2: loader e runner fake, com modos `off`, `observe` e `enforce`;
- P3: plugin Hermes;
- P4: plugin OpenDesign;
- P5: outros harnesses apenas com contrato próprio;
- P6: canário, promoção e rollback.

O núcleo continua dono de autenticação, catálogo, schemas, HTTP, idempotência,
ledger, readback, redaction e transporte MCP. O plugin só avalia contexto e
política do harness. Ele não faz HTTP, não repete mutações, não limpa ledger,
não escolhe credenciais e não fabrica autorização humana.

Critérios para qualquer plugin:

- seleção somente pela configuração do operador;
- caminho autorizado, sem traversal nem `shell=True`;
- timeout e limite de saída validados no startup;
- entrada/saída JSON versionada e sem segredos;
- `enforce` fail-closed antes do I/O;
- `observe` explicitamente não bloqueante;
- hash/versão registrados na evidência;
- prova positiva, negativa e de contexto ausente;
- rollback para `off` comprovado.

## v0.5.x — runtime compartilhado e escala operacional

### Broker Plane MCP compartilhado

O objetivo é reduzir a multiplicação de processos stdio observada quando o
OpenCode cria uma instância por contexto:

```text
Hermes / Codex / OpenCode
          ↓
cliente MCP local ou proxy
          ↓
broker Plane MCP compartilhado
          ↓
Plane API
```

Entregas propostas:

- broker local com autenticação entre clientes e broker;
- multiplexação de requests sem misturar sessões ou perfis;
- ledger e observabilidade centralizados, preservando isolamento lógico;
- limites de concorrência, backpressure e shutdown gracioso;
- health/readiness e métricas de processos, memória e latência;
- compatibilidade stdio durante migração;
- canário por cliente antes da promoção;
- rollback imediato para launchers versionados individuais.

A otimização não pode alterar o contrato de autorização, os escopos dos
clientes, o idempotency key, o readback nem a proveniência da operação.

### Operação e evolução da linha 0.5

- capability denominator verificável por cliente/runtime;
- diagnóstico de drift entre launcher, pacote, skill e contrato;
- observabilidade de chamadas, readbacks, estados desconhecidos e
  reconciliações sem conteúdo sensível;
- matriz de compatibilidade por versão de cliente;
- benchmark comparando processos individuais contra broker;
- teste de falha do broker, reconexão e preservação de sessões;
- runbook de promoção, rollback e quarentena.

## Sequenciamento e controle

Cada problema deve ser uma issue independente no GitHub, com critérios de
aceite e prova próprios. A sequência de release deve ser:

1. issue funcional ou de qualidade;
2. implementação e testes focalizados;
3. revisão do diff e invalidação da prova anterior;
4. suíte completa e gate de contrato;
5. build/instalação isolada;
6. tag imutável e readback remoto;
7. atualização da skill somente pelo instalador oficial.

Nenhuma fase 0.4 ou 0.5 deve ser descrita como implementada antes de possuir
código, testes, artefato e verificação correspondentes.
