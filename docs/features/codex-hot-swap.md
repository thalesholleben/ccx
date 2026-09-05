# Hot-swap de conta do Codex

## Purpose

Permitir que `ccx_codex switch` e `ccx_codex auto` troquem a conta usada pela
extensão Codex do VS Code sem encerrar o app-server persistente e sem cortar um
turno que já está executando.

## Scope

O recurso é opt-in, Windows e limitado a um app-server por `CODEX_HOME`. Ele usa o
protocolo JSON-RPC oficial do app-server e a configuração
`chatgpt.cliExecutable` da extensão para inserir um bridge stdio local.

Arquivos principais:

- `ccx_codex_bridge.py`: proxy JSONL, barreira de turnos, controle local e refresh;
- `ccx_codex_bridge_launcher.cs`: launcher `.exe` exigido pelo `spawn` da extensão;
- `install-ccx-codex-bridge.ps1`: instalação e rollback da configuração;
- `ccx_codex.py`: transação entre autenticação em memória, store e `auth.json`;
- `test_ccx_codex_bridge.py`: protocolo, segurança, launcher, instalador e runtime.

## Invariants

- Um turno em voo nunca muda de identidade. A troca espera `turn/completed`,
  inclusive quando o turno termina como `failed` ou `interrupted`.
- Enquanto há troca pendente, novos `turn/start`, `turn/steer`, `review/start`,
  `thread/compact/start`, `thread/shellCommand`, `thread/queue/add` e
  `thread/queue/start` aguardam em FIFO no stdio.
- `auth.json` e `last_switch` só avançam depois de `account/login/start` responder
  com sucesso. Falha na escrita local dispara rollback antes de liberar trabalhos.
- O callback `account/chatgptAuthTokens/refresh` só atende o `account_id` externo
  atual. Ele nunca troca de conta no meio de uma repetição causada por 401.
- Refresh token e id token nunca atravessam o socket de controle. Frames completos
  não são registrados, pois podem conter JWT ou conteúdo do usuário.
- Mais de um bridge vivo no mesmo `CODEX_HOME` bloqueia a troca inteira; não existe
  sucesso parcial.
- `account/logout` não faz parte do fluxo: além do gap de autenticação, pode revogar
  o grant guardado pelo CCX.

## Protocol

O app-server usa UTF-8 JSONL sobre stdio. O bridge encaminha os frames sem
reinterpretar conteúdo, preserva o tipo exato de cada request ID e consome apenas
IDs que ele próprio gerou. No `initialize`, conserva `clientInfo` e garante somente
`params.capabilities.experimentalApi = true`.

A troca injeta:

```json
{
  "method": "account/login/start",
  "id": "ccx-live:<id-aleatorio>",
  "params": {
    "type": "chatgptAuthTokens",
    "accessToken": "<access-token>",
    "chatgptAccountId": "<account-id>"
  }
}
```

O método e o fluxo de refresh estão descritos na
[documentação oficial do Codex app-server](https://developers.openai.com/codex/app-server/).
`chatgptAuthTokens` exige a capability experimental; por isso esta integração é
tratada como opt-in e pode precisar de adaptação se o contrato oficial mudar.

## Local control

Cada bridge abre uma porta aleatória em `127.0.0.1` e cria um registro com PID,
marca de criação do processo, `CODEX_HOME`, porta e segredo aleatório de 256 bits em
`~/.ccx/codex_bridges/`. O registro não contém token. O diretório herda a proteção
do perfil do usuário; o protocolo ainda exige comparação constante do segredo e
limita cada frame a 64 KiB.

Essa fronteira protege contra outros usuários e conexões acidentais. Um processo já
executando como o mesmo usuário pode ler `auth.json` diretamente e, portanto, já
está dentro da mesma fronteira de confiança.

## Happy Path

1. O monitor escolhe um slot e renova seu access token antes da troca, se necessário.
2. O cliente CCX encontra exatamente um registro vivo para o `CODEX_HOME` atual.
3. O bridge fecha a entrada para novos trabalhos e espera todos os turnos ativos.
4. O app-server aceita `chatgptAuthTokens` para a conta alvo.
5. Ainda com a barreira fechada, o CCX grava `auth.json`, o store e `last_switch`.
6. O cliente envia `commit`; o bridge libera os frames que estavam aguardando.

Sem bridge vivo, o passo 5 continua funcionando como antes e a mensagem deixa claro
que a conta valerá apenas para novas sessões.

## Installation

Pré-requisitos: Windows, Python 3.10+, extensão oficial `openai.chatgpt` x64 e o
compilador C# que acompanha o .NET Framework do Windows.

```powershell
.\install-ccx-codex-bridge.ps1
```

O instalador:

1. localiza a extensão ativa pelo manifesto do VS Code;
2. valida `package.json` e a assinatura Authenticode da OpenAI;
3. compila `~/.ccx/bin/ccx-codex-bridge.exe`;
4. configura `chatgpt.cliExecutable` de forma cirúrgica no JSONC;
5. acrescenta a chave a `settingsSync.ignoredSettings` para não sincronizar um
   caminho absoluto local com outras máquinas;
6. preserva o arquivo anterior ao lado das settings como
   `settings.json.ccx-codex-bridge.bak` até um uninstall bem-sucedido.

Ele recusa WSL e um `chatgpt.cliExecutable` preexistente de outro software. A
extensão marca essa setting como restrita e voltada a desenvolvimento; ela não é uma
API estável. O launcher resolve o `codex.exe` da extensão atual pelo `PATH` a cada
execução, usando o caminho instalado apenas como fallback após auto-update.

O instalador não reinicia o VS Code. Execute `Developer: Reload Window` uma vez,
somente quando puder encerrar a sessão atual.

## Validation

```powershell
python test_ccx.py
python test_ccx_codex.py
python test_ccx_profile.py
python test_ccx_codex_bridge.py
python ccx_codex.py switch N
```

Resultado esperado do último comando com uma janela já recarregada:

```text
slot N ativo (app-server atualizado)
```

A suíte do bridge usa credenciais sintéticas para confirmar que o app-server real
aceita dois logins externos no mesmo processo. Ela não envia prompt nem chama modelo.
Também verifica stdin/stdout/stderr, argv, exit code e que o Job Object encerra o
filho se a extensão matar o launcher.

## Edge Cases

- **Turno maior que 45 s:** a tentativa falha sem alterar disco; o monitor tenta de
  novo na checagem seguinte.
- **Dois app-servers no mesmo home:** a tentativa é recusada. Use `ccx_profile.py`
  para dar um `CODEX_HOME` a cada agente.
- **Bridge ausente:** só novas invocações leem a conta gravada em `auth.json`.
- **Refresh simultâneo:** o primeiro rotaciona o refresh token; os seguintes
  reutilizam o access token já atualizado, evitando `refresh_token_reused`.
- **Extensão atualizada:** o launcher prefere o novo binário oficial encontrado no
  `PATH`; o sidecar antigo é apenas fallback.
- **Falha depois do login e antes do commit:** o bridge reaplica a conta anterior e
  só então abre a barreira.

## Rollback

```powershell
.\install-ccx-codex-bridge.ps1 -Uninstall
```

O uninstall remove apenas o executable/config/state do CCX e suas duas alterações
de settings. Depois de restaurar as settings, remove o backup persistente. Se
`chatgpt.cliExecutable` tiver sido alterado depois da instalação, o valor do usuário
e o backup são preservados e o comando falha, sem apagar artefatos. Nenhum processo
é encerrado automaticamente; faça um reload manual para voltar ao app-server direto.

## Non-Goals

- migrar uma requisição que já começou;
- balancear vários agentes dentro do mesmo app-server;
- compartilhar uma conta diferente por thread;
- contornar limites, revogar login ou automatizar login por navegador;
- oferecer o bridge no WSL, Linux ou macOS nesta versão.
