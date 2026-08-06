param([Parameter(Mandatory=$true)][string]$Payload)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
  $source = [System.Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Payload))
  $tokens = $null
  $errors = $null
  $ast = [System.Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$errors)
  if ($errors.Count -gt 0 -or $ast.ParamBlock -ne $null -or $ast.BeginBlock -ne $null -or $ast.ProcessBlock -ne $null -or $ast.EndBlock.Traps.Count -gt 0 -or $ast.UsingStatements.Count -gt 0) { exit 2 }
  foreach ($token in $tokens) { if ($token.Text -eq '--%') { exit 2 } }
  $commands = [System.Collections.Generic.List[object]]::new()
  foreach ($statement in $ast.EndBlock.Statements) {
    if ($statement -isnot [System.Management.Automation.Language.PipelineAst]) { exit 2 }
    foreach ($element in $statement.PipelineElements) {
      if ($element -isnot [System.Management.Automation.Language.CommandAst]) { exit 2 }
      if ($element.Redirections.Count -gt 0) { exit 2 }
      if ($element.InvocationOperator -ne [System.Management.Automation.Language.TokenKind]::Unknown) { exit 2 }
      $words = [System.Collections.Generic.List[string]]::new()
      foreach ($part in $element.CommandElements) {
        if ($part -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
          $words.Add([string]$part.Value)
        } elseif ($part -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -and $part.NestedExpressions.Count -eq 0) {
          $words.Add([string]$part.Value)
        } elseif ($part -is [System.Management.Automation.Language.ConstantExpressionAst]) {
          $words.Add([string]$part.Value)
        } elseif ($part -is [System.Management.Automation.Language.CommandParameterAst] -and $part.Argument -eq $null) {
          $words.Add('-' + $part.ParameterName)
        } elseif ($part -is [System.Management.Automation.Language.CommandParameterAst] -and ($part.Argument -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $part.Argument -is [System.Management.Automation.Language.ConstantExpressionAst])) {
          $words.Add('-' + $part.ParameterName)
          $words.Add([string]$part.Argument.Value)
        } else { exit 2 }
      }
      if ($words.Count -eq 0) { exit 2 }
      $commands.Add($words.ToArray())
    }
  }
  if ($commands.Count -eq 0) { exit 2 }
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  [Console]::Out.Write(($commands.ToArray() | ConvertTo-Json -Compress -Depth 3))
} catch { exit 2 }
