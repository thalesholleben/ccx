# Security

## Modelo de ameaça

O `ccx` le e escreve tokens OAuth reais (Claude Code e Codex CLI) em arquivos
locais. Pontos que importam para quem for auditar ou confiar no codigo:

- **Nenhuma rede alem do provedor oficial.** `ccx.py` fala so com
  `api.anthropic.com` (leitura de cota) e `platform.claude.com` (refresh de
  token). `ccx_codex.py` fala so com `chatgpt.com` (leitura de cota) e
  `auth.openai.com` (refresh de token). Sem telemetria, sem terceiros.
- **Sem ping de aquecimento.** A leitura de cota nunca manda prompt nem abre
  janela de uso. Ver a secao "Termos de uso" do `README.md`.
- **Estado local fica em `~/.ccx/accounts.json` e `~/.ccx/codex_accounts.json`**,
  contendo os tokens OAuth de cada conta cadastrada. Esse diretorio nunca deve
  ser versionado, copiado para fora da maquina ou compartilhado.
- **Trocas de credencial sao cirurgicas e atomicas**: reescrevem so os campos
  de identidade, via arquivo temporario + `os.replace`, para nunca deixar um
  `.credentials.json`/`auth.json` truncado no meio de uma escrita.
- **Sem lock cooperativo confirmado com o Codex CLI real** (diferente do
  modulo Claude, que segura o lock de diretorio documentado no proprio
  codigo do Claude Code). Ver a secao "Modulo Codex" do `README.md` para o
  detalhe dessa janela de corrida conhecida.

## Reportando uma vulnerabilidade

Abra uma [GitHub Security Advisory](../../security/advisories/new) neste
repositorio, ou um issue normal se nao for algo sensivel. Sem SLA formal:
projeto pessoal mantido no tempo livre, mas relatos sao lidos e respondidos.
