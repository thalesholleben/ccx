# ccx

Monitora a cota das contas Claude Code e troca de conta antes de bater o limite.

Python stdlib puro, sem instalar nada. Nasceu para resolver um incômodo concreto:
com duas assinaturas Pro, você fica monitorando na mão qual delas ainda tem cota,
fazendo `/logout` e `/login` no meio do trabalho, e ainda desperdiça cota semanal
que ia expirar sem ser usada.

**Não existe ping de aquecimento aqui, de propósito.** A leitura de cota não manda
prompt nenhum. Ver [Termos de uso](#termos-de-uso).

Também tem um módulo para contas do Codex CLI (ChatGPT), ver
[Módulo Codex (ccx_codex)](#módulo-codex-ccx_codex).

> [!IMPORTANT]
> O modo de troca atual escreve a credencial **global** do cliente. Ele serve
> para uso sequencial ou para várias sessões que deliberadamente compartilham
> uma única identidade; não é um balanceador por agente e não migra com
> segurança processos persistentes já abertos. Para contas simultâneas, o
> padrão suportado pelos clientes é iniciar cada processo com um perfil isolado:
> [`CLAUDE_CONFIG_DIR`](https://code.claude.com/docs/en/env-vars) no Claude e
> [`CODEX_HOME`](https://developers.openai.com/codex/auth#credential-storage)
> no Codex. O `ccx` ainda não orquestra esses perfis.

---

## Sumário

- [Pré-requisitos](#pré-requisitos)
- [Setup](#setup)
- [Comandos](#comandos)
- [Estratégias de troca](#estratégias-de-troca)
- [Intervalo de checagem](#intervalo-de-checagem)
- [Monitor contínuo no Windows](#monitor-contínuo-no-windows)
- [No VS Code](#no-vs-code)
- [Como funciona por dentro](#como-funciona-por-dentro)
- [Problemas conhecidos e como diagnosticar](#problemas-conhecidos-e-como-diagnosticar)
- [Limitações](#limitações)
- [Termos de uso](#termos-de-uso)
- [Teste](#teste)
- [Módulo Codex (ccx_codex)](#módulo-codex-ccx_codex)

---

## Pré-requisitos

Duas ou mais contas Claude Code, e cada uma precisa ter sido logada pelo menos uma
vez nesta máquina para o `add` capturar a credencial.

**A pré-condição que realmente importa:** as contas precisam estar em organizações
diferentes. Contas com o mesmo `organizationUuid` compartilham um pool de cota só,
então trocar entre elas não resolve nada. O `add` avisa quando detecta isso. Para
conferir na mão, `oauthAccount.organizationUuid` em `~/.claude.json`.

Plataforma: Windows e Linux funcionam. No macOS o Claude Code guarda a credencial no
Keychain e o `add` não encontra (ver [Limitações](#limitações)).

## Setup

```bash
# logue com a primeira conta no Claude Code, depois:
python ccx.py add

# /logout, /login com a segunda conta, depois:
python ccx.py add

# confira
python ccx.py status
```

O `add` é idempotente: rodar de novo com a mesma conta ativa atualiza o slot
existente em vez de criar um duplicado, e limpa a marca de token morto se houver.

## Comandos

| Comando | O que faz |
| --- | --- |
| `ccx add [slot]` | Captura a conta logada agora. Sem argumento, usa o próximo número livre |
| `ccx status` | As contas lado a lado: 5h, 7d, resets e recomendação; reutiliza a leitura recente |
| `ccx stats` | Status consolidado de todas as contas Claude Code e Codex CLI |
| `ccx switch [slot]` | Troca manual. Sem argumento, rotaciona para a próxima |
| `ccx auto` | O monitor: acompanha a cota e troca sozinho |
| `ccx hook` | Checagem silenciosa para o evento `Stop` do Claude Code |

Flags globais, válidas em qualquer subcomando:

| Flag | Padrão | Efeito |
| --- | --- | --- |
| `--strategy consume-first\|best` | `consume-first` | Ver [Estratégias](#estratégias-de-troca) |
| `--threshold N` | `85` | Percentual em que a conta deixa de ser candidata |

Flags do `auto`:

| Flag | Padrão | Efeito |
| --- | --- | --- |
| `--cooldown N` | `300` | Segundos mínimos entre duas trocas, evita pingue-pongue |
| `--poll N` | `0` | Força intervalo fixo em segundos. `0` usa o dinâmico |
| `--once` | | Uma checagem só e sai, para uso em agendador |

Códigos de saída do `--once`: `0` trocou, `1` erro de configuração, `2` nada a
fazer, `3` todas as contas travadas.

**Uma instância por máquina.** Se já existe um `auto` rodando, o segundo avisa e sai
com código 0. Sem isso, abrir a pasta em três janelas do VS Code subiria três loops
triplicando o tráfego e disputando a mesma troca.

`status`, `hook` e `auto` também compartilham um cache em disco. Rodar `status`
várias vezes seguidas não gera uma nova consulta por conta a cada execução.

## Monitor contínuo no Windows

Para a rotação continuar sem depender de uma janela do VS Code, instale uma vez o
monitor do usuário atual:

```powershell
cd C:\caminho\para\ccx
.\install-ccx-monitor.ps1
```

Ele cria a tarefa `\CCX\Claude Monitor`, inicia-a agora e a inicia em cada logon. A
tarefa executa um watchdog a cada minuto: ele só lê o lock local do monitor
(não consulta usage) e relança o `ccx auto` em processo destacado se ele morrer. Ela
continua funcionando na bateria. O lock interno registra o PID do dono: processo
morto é retomado sem esperar o timeout, enquanto um processo apenas suspenso não
ganha um segundo monitor na retomada. Os eventos de início, erro inesperado,
encerramento manual, relançamento e troca ficam em
`~/.ccx/auto.log` (rotacionado em 512 KB). O arquivo nunca registra tokens.

O `ccx auto` do VS Code fica como fallback manual: não inicia mais ao abrir a pasta,
evitando que uma instância efêmera ocupe o lock antes do monitor permanente. Para
remover a tarefa:

```powershell
.\install-ccx-monitor.ps1 -Uninstall
```

## Estratégias de troca

Primeiro, dois conceitos que não são a mesma coisa:

- **Utilizável:** a janela que aperta está abaixo de 100%. A conta consegue atender.
- **Candidata:** a janela que aperta está abaixo do `--threshold`. Vale trocar para ela.

"Janela que aperta" é a maior entre 5h e 7d, porque é a que bloqueia primeiro. Uma
conta com 5h em 0% e semanal em 97% está tão bloqueada quanto o contrário.

**`consume-first`** (padrão): entre as candidatas, escolhe a de reset semanal mais
próximo. Cota semanal é perecível, então a lógica é gastar primeiro a que vai virar
pó. Troca mais, aproveita mais.

**`best`**: entre as candidatas, escolhe a de maior folga. Troca menos, mas
desperdiça cota semanal que ia expirar.

**Quando nenhuma conta é candidata**, cai para a utilizável de menor utilização. O
limiar existe para trocar antes de bater, não para te deixar parado numa conta
travada tendo outra que ainda atende. Se todas estiverem em 100%, aí não há o que
fazer e ele avisa.

Conta com cota desconhecida (erro de rede, token morto) nunca é escolhida
automaticamente, mas segue sendo alvo válido de um `switch` explícito.

Um erro ao medir a **conta ativa**, inclusive `HTTP 429`, não prova que uma chamada
ao modelo esgotou a cota. Sem leitura anterior confiável, o monitor mantém a conta
atual. Se ela foi confirmada como esgotada nos últimos 5 minutos e a releitura deu
429, essa última leitura ainda pode orientar a troca para uma conta conhecida — sem
depender de um erro de medição isolado.

## Intervalo de checagem

Polling só serve para decidir uma troca, e decisão de troca exige **duas contas
utilizáveis**. Todo o intervalo sai dessa observação:

**Duas ou mais utilizáveis:** faixa com jitter, 180 a 240s normalmente, apertando
para 100 a 120s quando a janela de **5h da conta ativa** passa de 70%.

O gatilho é o 5h de propósito. É a única janela que se move rápido dentro de uma
sessão. O semanal sobe devagar e leva dias para resetar, então usá-lo aqui
prenderia o poll na faixa apertada por dias inteiros sem que nada estivesse por
acontecer. Um poll de 4 minutos não perde nada de relevante no semanal.

**Menos de duas utilizáveis:** não existe decisão a tomar até alguma resetar, e o
`resets_at` da API diz exatamente quando. Dorme até lá em vez de checar, com teto de
1h (seguro barato caso o horário venha errado ou mude) e piso de 60s (evita busy
loop se o `resets_at` estiver no passado por dado velho). Na prática isso troca umas
25 requisições por 1.

Uma conta travada volta a atender quando **todas** as suas janelas em 100% resetarem,
então quem manda é o reset mais tarde entre elas. Entre contas travadas, acorda
quando a primeira volta.

**Erro em qualquer conta:** cai para a faixa larga. Se o endpoint devolveu 429,
insistir de 100 em 100s só piora.

**Cache compartilhado:** uma leitura válida é reaproveitada por até 30s; um erro,
por até 120s. O cache fica no mesmo store protegido por lock e é relido depois de
adquirir o lock. Assim, se quatro agentes terminarem juntos, o primeiro consulta e
os outros reutilizam o resultado em vez de fazer quatro varreduras completas, sem
atrasar o próximo poll dinâmico.

O jitter também evita um batimento perfeitamente periódico, que é padrão mais fácil
de detectar do que poll irregular.

### Checagem ao terminar cada resposta

Polling sozinho tem uma janela inevitável: uma resposta longa pode fazer a cota
saltar entre duas consultas. O comando `ccx hook` existe para o evento `Stop` do
Claude Code e pede uma checagem assim que cada resposta termina. Se a leitura
compartilhada ainda estiver recente, ele não consulta a rede novamente.

O hook é silencioso, respeita o mesmo cooldown e usa o mesmo `store.lock` do
monitor. Ele não manda prompt e não consome cota. O `auto` continua rodando como
fallback para resets e mudanças fora de uma sessão.

Configuração de usuário em `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/caminho/para/ccx.py\" hook",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

Hooks são carregados ao abrir a sessão. Depois de instalar ou alterar essa
configuração, encerre e abra o Claude Code novamente.

## No VS Code

Este repositório não instala uma task automática do VS Code. No Windows, a opção
sem supervisão para Claude é o
[monitor do Agendador](#monitor-contínuo-no-windows), independente da janela do
editor.

Use `Tasks: Run Task` → `ccx auto (fallback manual)` somente quando o monitor
permanente não estiver instalado ou durante diagnóstico. Não é preciso executar a
task a cada troca, a cada `status` nem quando um agente termina.

Não mantenha `ccx_codex auto` junto da extensão do Codex: a extensão usa um processo
persistente que pode continuar com a identidade carregada na inicialização mesmo
depois de `auth.json` mudar.

Fora do VS Code, `ccx-auto.cmd` faz o mesmo com um duplo clique, de qualquer
diretório.

## Como funciona por dentro

### De onde vem a cota

`GET https://api.anthropic.com/api/oauth/usage`, com `Authorization: Bearer <token>`
e o header `anthropic-beta: oauth-2025-04-20`. É a mesma fonte que o `/usage` do
Claude Code consome.

Leitura pura: não manda prompt, não consome cota e **não abre a janela de 5h**. A
resposta traz `five_hour` e `seven_day`, cada um com `utilization` (percentual) e
`resets_at` (ISO 8601).

### O que a troca escreve

Dois arquivos, e nos dois ela é cirúrgica:

| Arquivo | O que muda |
| --- | --- |
| `~/.claude/.credentials.json` | Só o bloco `claudeAiOauth` e o `organizationUuid` |
| `~/.claude.json` | Só o `oauthAccount` (a identidade que o Claude Code exibe) |

**Por que cirúrgica e não copiando o arquivo:** o `.credentials.json` guarda também
`mcpOAuth`, onde vivem os tokens OAuth dos servidores MCP. Uma troca que sobrescreve
o arquivo inteiro derruba esses logins toda vez. Existe teste que falha se isso
regredir (`test_swap_preserva_mcp`).

Toda escrita é atômica: arquivo temporário e `os.replace`, para nunca deixar um
JSON truncado se o processo morrer no meio.

### O lock, que é a parte séria

Toda escrita acontece segurando o lock do próprio Claude Code. O protocolo, lido do
código dele:

- O artefato é um **diretório** em `<alvo>.lock` (`~/.claude.lock`,
  `~/.claude.json.lock`). A atomicidade do `mkdir` é o mutex.
- Considera-se morto quando o mtime passa de 10s. Quem segura toca o mtime a cada
  3s para provar que está vivo, e um lock morto pode ser tomado.
- O Claude Code tenta 5 vezes com sleeps de 1 a 2s antes de desistir, então segurar
  por meio segundo é totalmente cooperativo.

**Sem isso a ferramenta falha em silêncio.** O fluxo de refresh do Claude Code é ler
a credencial, ir na rede e salvar, tudo sob `~/.claude.lock`. Uma troca que caísse
dentro dessa janela seria sobrescrita pelo token da conta velha, e você não veria
erro nenhum, só continuaria na conta errada. Sob o lock, a releitura dele enxerga a
credencial nova (não expirada) e ele aborta o próprio refresh.

O mesmo mecanismo de lock é reusado para a trava de instância única do `auto`, em
`~/.ccx/auto.lock`, com timeout 0 para falhar na hora em vez de esperar.
Somente os locks internos do CCX (`auto` e `store`) recebem um arquivo de dono com
PID; os locks do próprio Claude Code continuam vazios e seguem o protocolo original.

### Concorrência entre os próprios comandos

Existe um segundo lock, `~/.ccx/store.lock`, separado do lock de instância única
do `auto`. Ele serializa **toda** operação que renove token ou grave o store:
`collect` (usada por `status` e pelo `auto`), `switch` e `add`.

Isso não é zelo teórico, é cicatriz. A trava de instância única protegia só o
loop do `auto`, mas `status` também renova token e grava. Rodar `ccx status` num
terminal enquanto o `auto` rodava fazia os dois renovarem o mesmo refresh token
ao mesmo tempo. A rotação no servidor invalida a cópia de quem perdeu a corrida,
e a credencial guardada morre com `invalid_grant`, sem erro visível até você
tentar voltar para aquela conta e descobrir que ela não existe mais.

O mesmo ponto também protege o cache de usage. Depois de conseguir o lock, cada
processo relê o store antes de decidir se consulta a rede. Isso importa porque um
hook pode ter carregado o arquivo enquanto outro ainda estava consultando; sem a
releitura, os dois fariam a mesma chamada mesmo estando serializados.

`do_switch` faz a mesma releitura antes da escrita final. Isso evita que uma decisão
já calculada apague o refresh token ou o cache que outro hook gravou enquanto ela
esperava o lock.

Pelo mesmo motivo, `apply_slot` segura os **dois** locks do Claude Code durante a
troca inteira, em vez de um por escrita. Entre gravar a credencial e gravar o
perfil existe um instante em que o token é de uma conta e o `oauthAccount` é de
outra. Quem lesse a identidade nessa janela concluiria a conta errada, e o
`sync_active_slot` seguinte copiaria o token de uma conta para o slot da outra.

Ordem de aquisição, sempre a mesma para não travar: `store.lock` primeiro, depois
os locks do Claude Code.

### Leitura de arquivo: corrompido não é vazio

`read_json` devolve `{}` só quando o arquivo **não existe**. JSON inválido levanta
`CorruptFile` e aborta a operação.

Confundir os dois é destrutivo: o passo seguinte reescreve o arquivo e leva junto
o que não foi lido. Num `.credentials.json` truncado por uma escrita interrompida,
isso apagaria o `mcpOAuth` inteiro.

Pela mesma lógica, uma resposta 200 do endpoint de usage sem nenhuma janela
reconhecível levanta em vez de virar `{}`. Um dicionário vazio faria a conta
aparecer com 0% de uso, ou seja, ser eleita como a mais folgada de todas
justamente por não sabermos nada sobre ela.

### Identidade da conta ativa, e por que não pelo token

O slot ativo é identificado por **(e-mail, `organizationUuid`)** lidos do
`oauthAccount` em `~/.claude.json`, nunca comparando tokens.

O Claude Code rotaciona o refresh token da conta que está usando. Se a
identidade dependesse do token, ela se perderia na primeira rotação, e aí
**todo slot pareceria inativo, inclusive o vivo**. A consequência é a pior
possível: o `ccx` passaria a renovar o token da conta ativa por baixo do Claude
Code, os dois disputariam a rotação, e um invalidaria o refresh token do outro,
matando a credencial guardada.

Pelo mesmo motivo, a cada checagem a credencial viva é copiada de volta para o
slot ativo (`sync_active_slot`). Sem isso a cópia guardada envelhece enquanto a
conta é usada, e quando você tentasse voltar para ela o refresh responderia
`invalid_grant`.

### Certificados: o `ccx` prefere o bundle do certifi

O armazenamento de certificados do Windows carrega raízes legadas expiradas, e a
cadeia da Let's Encrypt falha por ali. Isso atinge `platform.claude.com`, onde
fica o endpoint de refresh, mas **não** atinge `api.anthropic.com`, que usa outra
CA. O sintoma é traiçoeiro: a leitura de cota funciona normalmente e só o refresh
quebra, então a conta inativa vai apodrecendo sem erro visível até morrer.

Por isso o contexto TLS usa `certifi.where()` quando o pacote está disponível, e
cai no padrão do sistema quando não está. Para conferir:

```bash
python -c "import ssl,socket; ssl.create_default_context().wrap_socket(socket.create_connection(('platform.claude.com',443)),server_hostname='platform.claude.com')"
```

Se isso levantar `CERTIFICATE_VERIFY_FAILED`, o `certifi` deixa de ser opcional:
`pip install -U certifi`.

### Regras de refresh de token

- **A conta ativa nunca é renovada.** O Claude Code é dono dela. Se os dois
  renovarem, um invalida o token do outro.
- **A inativa é renovada quando expira**, senão não há como ler a cota dela. O POST
  vai para `https://platform.claude.com/v1/oauth/token` com
  `grant_type=refresh_token` e o `client_id` público do Claude Code.
- **Refresh token rejeitado** (`invalid_grant` em 400, 401 ou 403) é permanente: o
  slot é marcado `MORTO`, sai da rotação e aparece assim no `status`. Insistir num
  token morto só gera ruído. Para recuperar, logue com aquela conta e rode
  `ccx add` de novo.
- **Qualquer outro erro é transitório** e volta a ser tentado na próxima checagem.

### Estado

`~/.ccx/accounts.json`, com os slots, e `last_switch` para o cooldown. Por slot:
e-mail, bloco `claudeAiOauth`, `oauthAccount` e `org_uuid`.

**Contém tokens OAuth. Nunca versione nem copie para fora da máquina.**

## Problemas conhecidos e como diagnosticar

**Saber se o `auto` está vivo:**

```bash
python -c "import ccx_watchdog; print('vivo' if ccx_watchdog.monitor_alive() else 'morto')"
```

Para os locks internos do CCX, PID vivo com heartbeat velho pode ser apenas uma
suspensão: o watchdog não duplica o monitor nesse caso. PID morto (ou lock legado
sem dono com mtime velho) é tomado automaticamente; não apague o lock na mão.

**Saber por que o monitor permanente reiniciou:** abra `~/.ccx/auto.log` e confira
o estado da tarefa `\CCX\Claude Monitor` no Agendador do Windows. O log registra
só eventos operacionais, nunca tokens.

**Conta aparece com `?` na cota.** O `status` mostra o motivo ao lado (`HTTP 429`,
`timeout`, `token morto`). Um 429 no endpoint de usage é armazenado por 120s e
derruba o poll para a faixa larga. Não repita `status` tentando fazê-lo sumir.

**Trocou mas o cliente continua na conta antiga.** Reinicie aquela sessão. Não há
garantia de recarga dinâmica para todo processo persistente. O Codex oficial, em
particular, mantém um snapshot em memória e não observa alteração externa de
`auth.json` até um reload explícito; também recusa cruzar a identidade da sessão
para outra conta/workspace sem reconstruir seu estado
([fonte](https://github.com/openai/codex/blob/4642370542739d5dd080b0c87a9de06a6435d3db/codex-rs/login/src/auth/manager.rs#L1769-L1780)).

## Limitações

- **macOS não é suportado no `add`:** lá o Claude Code guarda a credencial no
  Keychain, não em arquivo.
- **Reset banking do Codex não é usado.** A API da OpenAI expõe
  `rate_limit_reset_credits`, créditos redimíveis que resetam a janela atual
  antes da hora. O `ccx_codex` não lê nem redime esse crédito: redimir seria
  manipular o estado da conta, o mesmo motivo pelo qual não existe ping de
  aquecimento no módulo Claude. Fica para uma versão futura, só como leitura.
  Ver [Módulo Codex](#módulo-codex-ccx_codex).
- **Não considera limites semanais por modelo.** A API expõe isso num array
  `limits` com entradas `weekly_scoped`, que hoje é ignorado. Se você trabalha
  fixado num modelo e bate o limite dele antes das janelas gerais, esse ramo
  precisaria entrar na decisão.
- **A troca é global, não por agente.** Várias sessões abertas no mesmo perfil
  podem manter credenciais em memória, disputar refresh ou continuar na identidade
  anterior. Para paralelismo real, use processos separados desde o início com
  `CLAUDE_CONFIG_DIR`/`CODEX_HOME`; agentes internos de uma mesma sessão continuam
  compartilhando a conta daquela sessão.

## Termos de uso

A ferramenta lê a API de usage com token OAuth de assinatura, o que a cláusula 3.7
dos Consumer Terms da Anthropic trata como acesso automatizado fora de API key. Não
há caso conhecido de enforcement contra isso, e é o que qualquer status line de cota
faz.

O que gera banimento é outra coisa: harness de terceiro, spoofing do harness e
revenda de acesso. Nada disso acontece aqui, quem fala com o modelo continua sendo o
Claude Code oficial, e a ferramenta só reposiciona qual credencial ele usa. Ter
várias contas pagas não viola os termos, isso foi dito publicamente por engenheiro
da Anthropic.

Por isso também não existe ping periódico para abrir a janela de 5h mais cedo: seria
script disparando prompt em assinatura, o caso que a cláusula descreve ao pé da
letra. Se quiser alinhar a janela ao seu dia, manda a primeira mensagem você mesmo.

## Teste

```bash
python test_ccx.py
```

Sem framework, só `assert`. 31 testes cobrindo:

- escolha de conta nas duas estratégias, e o fallback quando nenhuma é candidata
- cálculo de intervalo nos dois ramos (faixa com jitter e sono até o reset)
- execução silenciosa da checagem usada pelo hook `Stop`
- continuidade do monitor após erro inesperado
- watchdog relançando somente monitor morto, sem duplicar processo suspenso
- retomada com `timeout=0` removendo lock morto sem tomar lock vivo
- cache/debounce compartilhado, inclusive releitura depois de esperar o lock
- `HTTP 429` de usage mantendo a ativa sem histórico e trocando quando há
  confirmação recente de esgotamento
- troca relendo o store para não perder refresh token/cache concorrente
- saída do `status` sem intervalo enganoso quando há troca pendente
- preservação do `mcpOAuth` na troca, e identidade não vazando entre slots
- identidade sobrevivendo à rotação de token feita pelo Claude Code
- JSON corrompido abortando em vez de virar arquivo vazio
- respostas inesperadas da API (200 sem `access_token`, usage sem janelas)
- `resets_at` sem fuso horário
- validação de flags, liberação do lock e escrita atômica

Vários desses nasceram de bugs que só apareceram rodando em produção, não de
casos imaginados. Se um deles quebrar, foi regressão de algo que já falhou uma
vez.

## Módulo Codex (ccx_codex)

Mesma ideia do `ccx.py`, para contas do Codex CLI (ChatGPT). É um arquivo
separado (`ccx_codex.py`) que **importa `ccx.py`** e reusa de lá a engine de
decisão (`pick_target`, `band_delay`, `next_wake`), a formatação de `status` e
os primitivos de IO (leitura/escrita atômica, lock de diretório). O que muda é
só o que é genuinamente diferente entre Claude Code e Codex CLI: formato do
arquivo de credencial e protocolo da API de usage.

```bash
# logue com a primeira conta no Codex CLI, depois:
python ccx_codex.py add

# codex logout, codex login com a segunda conta, depois:
python ccx_codex.py add

# confira
python ccx_codex.py status

# veja Claude Code e Codex na mesma saída
python ccx.py stats

# somente para invocações sequenciais/frescas; não use com extensão/app-server aberto
python ccx_codex.py auto
```

Os comandos, flags e a leitura do `status`/`auto` são idênticos aos do
`ccx.py` (ver [Comandos](#comandos) e [Estratégias de troca](#estratégias-de-troca));
só troque `ccx.py` por `ccx_codex.py`. `ccx-codex-auto.cmd` faz o mesmo que
`ccx-auto.cmd`, para o Codex.

### O que é diferente do módulo Claude

- **Um arquivo só.** O Codex CLI guarda tudo em `~/.codex/auth.json`
  (`CODEX_HOME` para outro caminho), sem o equivalente ao `.claude.json`
  separado. A troca continua cirúrgica: só reescreve `tokens` e
  `last_refresh`, preservando `auth_mode`/`OPENAI_API_KEY` como estavam.
- **Token é JWT.** `access_token` e `id_token` são JWTs de verdade. A
  expiração vem do claim `exp` do `access_token` (decodificado localmente só
  para leitura, sem checar assinatura), e a identidade (e-mail, `account_id`,
  `workspace_id`) vem dos claims do `id_token`. Não existe um campo
  `expiresAt` gravado à parte como no `.credentials.json` do Claude Code.
- **Refresh:** `POST https://auth.openai.com/oauth/token` com
  `grant_type=refresh_token` e o `client_id` público do Codex CLI
  (`app_EMoamEEZ73f0CkXaXp7hrann`). Erros permanentes (`invalid_grant`,
  `refresh_token_expired`, `token_invalidated` etc., o conjunto documentado
  pela própria API) marcam o slot como `MORTO`, igual ao módulo Claude.
- **Usage:** `GET https://chatgpt.com/backend-api/wham/usage`, com
  `chatgpt-account-id` no header. A resposta traz `primary_window` (janela
  curta, ~5h) e `secondary_window` (semanal), mapeados para os mesmos rótulos
  `5h`/`7d` que o resto do código já entende, então a engine de decisão do
  `ccx.py` funciona sem nenhuma alteração.
- **Processos persistentes mantêm a identidade em memória.** O `AuthManager`
  oficial carrega `auth.json` uma vez, só observa mudanças externas após reload
  explícito e protege a identidade original da sessão. Portanto,
  `ccx_codex switch/auto` altera invocações **novas**, mas não migra com segurança
  a extensão, app-server ou agentes já abertos. Uma sessão antiga ainda pode
  tentar renovar o token anterior e terminar em 401
  ([código oficial](https://github.com/openai/codex/blob/4642370542739d5dd080b0c87a9de06a6435d3db/codex-rs/login/src/auth/manager.rs#L1769-L1780)).
  Para contas paralelas, use um `CODEX_HOME` isolado por processo. Essa
  orquestração ainda não faz parte do `ccx`.
- **Workspace em vez de organização.** O aviso de cota compartilhada usa
  `workspace_id` (seats de Team/Enterprise) em vez do `organizationUuid` do
  Claude Code.
- **Reset banking não implementado.** Ver [Limitações](#limitações).

### Origem

Este módulo nasceu de ler o código-fonte do
[codex-lb](https://github.com/Soju06/codex-lb) (load balancer de contas
ChatGPT, proxy completo com dashboard) para extrair o protocolo real:
endpoint de usage, endpoint e client_id de refresh, e o formato do
`auth.json`. A arquitetura de proxy dele não foi portada, o `ccx_codex`
continua com a filosofia do `ccx.py`: sem servidor, sem dependência, só
reposiciona a credencial que o Codex CLI oficial usa.

### Teste

```bash
python test_ccx_codex.py
```

Mesmo estilo do `test_ccx.py`, com 17 testes cobrindo o que é específico do Codex: leitura
de claims do JWT, expiração via `exp`, classificação de erro de refresh
(permanente vs. transitório), mapeamento de `primary_window`/`secondary_window`
para o formato `5h`/`7d`, preservação de `auth_mode`/`OPENAI_API_KEY` na
troca, identidade sobrevivendo à rotação de refresh token, cache compartilhado,
429 de usage com decisão baseada em histórico recente e troca sem sobrescrever
estado concorrente.

## Licença

[MIT](LICENSE).
