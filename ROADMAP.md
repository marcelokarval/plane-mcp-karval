# Roadmap 0.3.2 — plugins de políticas por harness

Status: proposta de implementação; nenhum loader ou plugin descrito aqui está
implementado ou ativado por este documento.

Este roadmap integra a documentação da 0.3.2. Não publica uma nova release nem
promete que todo o roadmap já pertence ao pacote executável dessa versão.

## Objetivo

Manter o Plane MCP simples, compartilhável e independente de harness. Regras
específicas de Hermes, OpenDesign, deepseek-harnss e futuros clientes ficam em
plugins locais, implementados como scripts com contrato versionado.

A skill `plane-mcp-operations` ensina o contrato comum. Cada plugin pode trazer
uma referência curta com suas regras próprias, sem replicar a skill inteira.

## Estado atual e fronteira

- `src/plane_mcp_karval/server.py` expõe as ferramentas e coordena chamadas.
- `src/plane_api/client.py` executa o contrato técnico Plane, ledger e readback.
- `src/plane_mcp_karval/cli.py` inicia o transporte e carrega o ambiente.
- A skill portátil já distingue política organizacional de capacidade MCP.
- Os novos diretórios, hooks e configurações abaixo são alvos de implementação,
  não APIs existentes. Não começar por reescrever o cliente ou criar outro MCP.

## Arquitetura alvo

```text
cliente MCP do harness
  → processo Plane MCP configurado pelo operador
  → plugin local selecionado para esse harness
  → validação técnica + reserva de idempotência + Plane API
  → readback + recibo persistido
  → observação pós-resultado pelo plugin
  → resposta MCP
```

### Núcleo comum: não delegar aos plugins

- Autenticação Plane e custódia das credenciais.
- Catálogo, schema, bindings de alvo, HTTP e paginação.
- Idempotência, tentativa única, ledger e reconciliação sem replay.
- Readback e distinção entre efeito aplicado, verificado, parcial e desconhecido.
- Transporte e superfície MCP estável; plugin não registra ferramentas arbitrárias.
- Redação segura de erros e proteção de dados.

### Plugins: regras e contexto do harness

- Pré-condições organizacionais de execução e transição de estado.
- Convenções de evidência, sessão, projeto de trabalho e comentários.
- Metadados públicos permitidos para proveniência e auditoria.
- Validação específica de contexto fornecido pelo host.

Não transferir regras genéricas de Plane para um plugin chamado Hermes. A presença
de um plugin não autentica o harness, e um nome de cliente informado pelo modelo
não comprova identidade ou autorização.

## Contrato mínimo de plugin v1 — proposto

### Seleção e carregamento

- Um plugin ativo por processo MCP no MVP; sem encadeamento/precedência entre vários.
- Seleção explícita na configuração local/launcher do operador. Não aceitar script,
  caminho, comando ou plugin escolhido por argumentos de uma ferramenta MCP.
- Manifesto local com `id`, `version`, `contract_version`, `entrypoint`, hooks,
  timeout, limite de saída e modo `off|observe|enforce`.
- Resolver entrypoint dentro da raiz autorizada do plugin; rejeitar traversal e
  symlink que escape dela. Invocar uma lista de argumentos, nunca `shell=True`.
- Sem download, instalação automática, descoberta de código em diretório de projeto
  ou hot reload durante uma operação. Mudança requer nova instância do processo.
- Registrar versão/hash do plugin carregado. Escolha de modo só pelo operador.

### Execução

Scripts subprocessados com JSON em stdin e exatamente um JSON em stdout.
Stderr é diagnóstico local limitado e sanitizado, nunca resposta bruta ao modelo.
Contrato independente de linguagem; implementação de referência em Python, sem
introduzir npm, servidor auxiliar ou framework de plugins para o MVP.

Entrada: versão do contrato, hook, operation_id, operação, alvo, campos mínimos do
payload necessários à regra e contexto permitido do host. Não enviar credenciais,
ambiente inteiro, logs, histórico bruto, hidden reasoning ou banco de sessões.

Saída de pré-validação: `decision=allow|deny`, códigos de motivo e metadados
permitidos. `allow` significa apenas aprovação pela política do plugin; não é
consentimento humano, credencial, nem dispensa validações do núcleo.

Sem reescrita silenciosa de workspace, projeto, item, estado, payload ou chave
idempotente. Se uma regra exige outro conteúdo, devolve orientação e bloqueia a
solicitação atual; o host apresenta uma nova intenção antes da escrita.

### Hooks iniciais

1. `before_operation`: valida política antes de reserva e I/O da operação. Pode
   receber snapshot previamente obtido pelo núcleo quando a regra o exigir;
   o plugin não faz sua própria chamada Plane. Leituras também podem ser negadas.
2. `after_readback`: recebe recibo sanitizado já persistido; pode produzir
   diagnóstico/metadados, não mudar o resultado factual nem escrever no Plane.

Operações offline de descoberta não devem depender de acesso ao provider ou de
um plugin saudável. Decidir no contrato a distinção entre catálogo offline e
leituras de dados antes de implementar o hook.

Não criar hook que execute HTTP, publique mensagens, repita mutações, limpe
ledger ou faça rollback. Necessidades de efeitos externos são escopo futuro,
não uma permissão implícita do plugin.

### Falhas, confiança e idempotência

- `off`: não executa plugin e preserva o comportamento MCP atual.
- `observe`: avalia e registra diagnóstico sem aplicar decisão; não é enforcement.
- `enforce`: plugin ausente, inválido, timeout ou decisão deny bloqueiam ANTES da
  escrita. Sem fallback silencioso para off/observe. Limites validados no startup.
- Falha pós-readback não converte uma escrita aplicada em "não executada"; devolver
  recibo verdadeiro com warning separado. Nunca repetir a operação para sanar hook.
- Fixar identidade/versão/hash do plugin no contexto da operação e na evidência
  do ledger, com compatibilidade de leitura dos registros antigos.
- Retry/reconciliação de operação existente não cria nova escrita nem aplica
  retroativamente política nova ao efeito antigo. Registrar a proveniência original
  e a configuração da nova observação separadamente.
- Subprocesso NÃO é sandbox. Plugins são código local confiável e revisado, executado
  com privilégios do usuário. Ambiente mínimo e ausência de token não impedem acesso
  a arquivos/rede do usuário. Isolamento OS real exige fase própria e prova.

## Estrutura proposta

```text
src/plane_mcp_karval/plugins/   # contrato, loader, runner e validação
plugins/
  hermes/                     # manifesto, script e regras Hermes
  opendesign/                 # manifesto, script e regras OpenDesign
  deepseek-harnss/             # nome fornecido pelo operador; confirmar ID técnico
skills/plane-mcp-operations/references/plugins.md
tests/                        # usar a árvore tests/ existente para a suíte
```

Não criar pastas vazias como suposta implementação. A grafia `deepseek-harnss`
é preservada do pedido; não assumir que é o identificador real da instalação.
Confirmar o contrato do harness antes de congelar seu manifesto.

## Plano de execução

| Etapa | Entrega | Aceite |
| --- | --- | --- |
| P1 — contrato | Schema de manifesto/I/O, pontos de hook e classificação das regras atuais | Exemplos válidos/inválidos testados; regra classificada como núcleo ou harness; zero gate legado transplantado sem justificativa |
| P2 — infraestrutura mínima | Loader e runner com plugin fake, modos, timeout e limites | Sem plugin mantém contrato atual; enforce bloqueia antes de I/O; observe não bloqueia; sem seleção por caller |
| P3 — Hermes | Script com regras realmente específicas, descobertas nos consumidores atuais | Positivo/negativo de contexto e prontidão; autorização vem do host, nunca receipt fabricado; sem dependência obrigatória para outros harnesses |
| P4 — OpenDesign | Script de contexto de execução/projeto e proveniência permitida | Apenas metadados comprovados; sem assumir sessão Hermes, projetos ou modelos a partir de rótulos de UI |
| P5 — deepseek-harnss | Confirmar identidade e adaptar contexto nativo do harness | Sem imitar metadados Hermes/OpenDesign; desconhecido fica explícito; casos positivos/negativos próprios |
| P6 — integração e promoção | Skill, guia de instalação, canário por harness e rollback | Mesma API MCP; plugin/mode/hash observáveis; sem efeitos extras; backup e retorno à configuração anterior comprovados |

Ordem: P1 → P2 → P3; P4 e P5 usam o contrato estabilizado; P6 encerra a entrega.
Não adicionar Codex ou outro harness por simetria: encaixar um novo plugin quando
suas regras específicas forem identificadas e a implementação for solicitada.

## Matriz mínima de prova

- Contrato MCP existente e suíte sem plugin continuam passando.
- Manifesto incompatível, plugin faltante, JSON inválido, saída excessiva, timeout,
  processo com exit não zero e caminho fora da raiz: falha limitada e previsível.
- Request do modelo não troca plugin, modo, caminho ou contexto confiável.
- Token e campos privados não entram no stdin/ambiente herdado/saída do plugin.
- Deny/enforce não envia requisição de escrita nem consome uma tentativa provider.
- Observe e off não são apresentados como enforcement.
- Falha pós-escrita preserva recibo, idempotência e possibilidade de reconciliação.
- Plugin não sobrescreve alvo, readback, timestamps históricos ou consentimento.
- Troca de versão do plugin não causa replay de operação nem invalida evidência antiga.
- Cada harness tem prova positiva, negativa e de contexto ausente.
- Canário live somente após autorização de alvo/ações; testes fake primeiro.

## Decisão e limites

Alternativas consideradas: if/else por harness no núcleo é mais rápido inicialmente,
mas acopla regras e repete os bloqueios antigos; imports Python in-process são
menores, mas acoplam linguagem/dependências e falhas ao servidor. Scripts locais
com I/O versionado são o compromisso escolhido: simples de distribuir, testáveis
e independentes de linguagem, ao custo de subprocesso e contrato explícito.

MVP não inclui marketplace, plugins remotos, instalação por prompt, múltiplos
plugins encadeados, sandbox presumido ou uma nova camada de consentimento.
O objetivo é separar política específica sem tornar o MCP um framework de harness.
