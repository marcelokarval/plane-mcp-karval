# AGENTS — Plane MCP Karval

## Objetivo e base

MCP Plane simples de operar, completo no catálogo e correto nos resultados.
Este checkout é a base exclusiva de desenvolvimento:
`/path/to/project`.
Instalações Hermes/Codex/OpenDesign são alvos de distribuição, não bases alternativas.
Leia `README.md` para instalação, comandos, verificação e recuperação.
Confirme configurações e runtime atuais; relatórios anteriores são históricos.

## Mapa do projeto

- `src/plane_mcp_karval/server.py`: ferramentas MCP públicas e argumentos.
- `src/plane_mcp_karval/cli.py`: transporte e carregamento do `.env`.
- `src/plane_mcp_karval/transport_policy.py`: HTTP offline; stdio com acesso live.
- `src/plane_api/client.py`: HTTP Plane, validação, recibos e readback.
- `src/plane_api/operation_registry.json`: catálogo e contratos de operações.
- `tests/`: regressões, matriz contratual e fluxo stdio com provider fake.
- `scripts/verify_mcp_release.py`: instalação/stdio e leitura live opcional.
- `scripts/verify_configured_clients.py`: smoke dos comandos dos três clientes.
- `scripts/reconcile_canary_absence.py`: reconciliação read-only de canário existente.

## Contratos essenciais

1. Não reintroduzir elicitation, consentimento duplicado, recibos de autorização
   fabricados ou planejamento obrigatório para mutações MCP comuns. O host/cliente
   confiável controla autorização e escopo; o MCP executa o contrato técnico.
2. Preservar autenticação, validação de alvo/payload, idempotência, tentativa única,
   ledger durável e readback. Resultado ambíguo não autoriza repetir escrita.
3. Distinguir `mutation_applied` de `readback_verified`. Reconciliação não repete
   escrita. Nunca apagar, zerar ou retroceder o ledger para desbloquear operação.
4. DELETE prova ausência do alvo, não conformidade documental de todos os itens
   sobreviventes. Preservar estrutura, identificação, bindings e paginação.
5. Compatibilidade com ACK mínimo de comentário e coleção sem `name` deve ser
   delimitada à operação pertinente; não relaxar todos os schemas.
6. HTTP 404 tipado no endpoint correto pode provar ausência. Erro MCP genérico,
   timeout ou HTTP 401/403/500 não provam exclusão.
7. Catálogo completo não significa que todas as operações foram exercitadas live.
8. Não transformar o HTTP offline em acesso remoto live ao Plane sem escopo
   e autenticação próprios.

## Segredos e distribuição

- `.env` pertence ao projeto, ignorado pelo Git, modo `0600`.
- Não copiar credenciais para código, wheel, manifestos, relatórios ou logs.
- Variáveis fornecidas pelo chamador têm precedência sobre `.env`.
- O launcher do wheel inicia no diretório canônico para carregar `.env`.
- Distribuir wheel em ambiente isolado com dependências do `uv.lock`.
- Não editar instalações legadas nem usar overlays/PYTHONPATH como atualização.
- Preservar backup e instalações anteriores até verificar a promoção.
- Rollback de código/configuração não desfaz dados Plane nem o ledger.

## Fluxo pragmático

1. Ler definição e usos afetados; confirmar o escopo solicitado.
2. Reproduzir o defeito com teste e corrigir a menor fronteira necessária.
3. Executar `uv run --frozen python -m pytest -q`, `uv lock --check` e
   `git diff --check`; não afirmar sucesso sem execução real.
4. Para promoção: gerar wheel atualizado, testar instalação limpa, registrar hash,
   fazer backup e verificar o comando efetivo de cada cliente.
5. Relatar resultado, prova e pendências de forma curta. Não reabrir arquitetura
   nem acrescentar etapas sem necessidade concreta.

Smoke de transporte/configuração não é turno de modelo nem atualização automática
de sessões já abertas. Reconectar o MCP quando necessário; não reiniciar serviços
indiscriminadamente. Canários live exigem alvo/ações autorizados e limpeza verificada.
Não repetir mutações já comprovadas para corrigir apenas um relatório.

Não fazer commit/push, limpar alterações locais, modificar outros projetos ou
alterar serviços/dados externos fora do escopo solicitado.
