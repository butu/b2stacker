#engine v8
// ----------------------------------------------------------------------------
// b2stacker – Startskript für PixInsight/WBPP (Weg B aus dem Konzept).
//
// Aufruf (keine Pfade auf der Kommandozeile außer dem Auftrag):
//   PixInsight.exe -n --automation-mode -r="<dieses Skript>,job=<auftrag.json>" --force-exit
//
// Der Auftrag (JSON, von b2stacker geschrieben) enthält:
//   outputDirectory  Ausgabeordner (in <workdir>/temp)
//   result           Ergebnisdatei, die dieses Skript schreibt
//   status           Statusdatei für das Event-Script (Fortschritt)
//   eventScript      Pfad zum Event-Script (optional)
//   sessionKeyword   Name des Gruppen-Schlüssels, der Lights und Flats einer Nacht verbindet
//   params           WBPP-Einstellungen als { engineEigenschaft: Wert }
//   indexed          WBPP-Einstellungen je Bildtyp, z. B. { "rejection": [null, null, null, 3] }
//   files            [{ path, type: bias|dark|flat|light, session? }]
//   dryRun           true = nur Gruppen bilden und melden, nicht rechnen
//
// Wichtig: PixInsight 1.9.6 führt Skripte ohne "#engine v8" in der ersten Zeile nicht aus.
// Echte Pfade, keine Links/Kopien; Kommas in Pfaden sind hier kein Problem.
// ----------------------------------------------------------------------------
// b2stacker setzt den Pfad beim Schreiben des Skripts ein (WBPP der verwendeten PixInsight-Installation):
#include "__WBPP_MAIN__"

function b2Arg( name )
{
   for ( let i = 0; i < Runtime.jsArguments.length; ++i )
   {
      let a = Runtime.jsArguments[ i ];
      if ( a.indexOf( name + "=" ) == 0 )
         return a.substring( name.length + 1 );
   }
   return undefined;
}

function b2Norm( p )
{
   return String( p ).replace( /\\/g, "/" ).toLowerCase();
}

function b2Run()
{
   let jobPath = b2Arg( "job" );
   let job = JSON.parse( File.readTextFile( jobPath ) );
   let out = { job: jobPath, started: ( new Date ).toISOString(), ok: false, phase: "start", added: 0, notAdded: [],
               groups: [], pixinsight: "", wbpp: "" };
   let save = () =>
   {
      try { File.writeTextFile( job.result, JSON.stringify( out, null, 1 ) ); } catch ( x ) {}
   };
   try
   {
      out.pixinsight = [ CoreApplication.versionMajor, CoreApplication.versionMinor, CoreApplication.versionRelease,
                         CoreApplication.versionRevision ].join( "." ) + " build " + CoreApplication.versionBuild;
      out.wbpp = BPP.Version.WBPP_VERSION;
      save();

      engine.id = BPP.Version.WBPP_ID;
      engine.title = BPP.Version.WBPP_TITLE;
      engine.version = BPP.Version.WBPP_VERSION;
      engine.fastMode = false;
      engine.loadSettingsKeyBase = BPP.Version.WBPP_SETTINGS_KEY_BASE;
      engine.saveSettingsKeyBase = BPP.Version.WBPP_SETTINGS_KEY_BASE;
      engine.automationMode = true;
      engine.preRunMessages = [];
      engine.groupsManager.clear();
      engine.parametersManager.setDefaultParameters();

      out.phase = "params";
      for ( let k in job.params || {} )
         engine[ k ] = job.params[ k ];
      for ( let k in job.indexed || {} )
      {
         let arr = job.indexed[ k ];
         for ( let i = 0; i < arr.length; ++i )
            if ( arr[ i ] !== null && arr[ i ] !== undefined )
               engine[ k ][ i ] = arr[ i ];
      }
      engine.outputDirectory = job.outputDirectory;
      engine.platesolveFallbackManual = false;
      engine.frameSelectionInteractive = false;
      engine.localNormalizationInteractiveMode = false;
      if ( job.sessionKeyword )
      {
         engine.groupingKeywordsEnabled = true;
         engine.keywords.add( job.sessionKeyword, BPP.KeywordMode.PRE );
      }

      out.phase = "files";
      let types = { bias: ImageType.Bias, dark: ImageType.Dark, flat: ImageType.Flat, light: ImageType.Light };
      let session = {};
      for ( let f of job.files )
      {
         let r = engine.addFile( f.path, types[ f.type ] );
         if ( r && r.success === false )
            out.notAdded.push( { path: f.path, message: String( r.message ) } );
         else
            out.added++;
         if ( f.session )
            session[ b2Norm( f.path ) ] = f.session;
      }
      if ( job.sessionKeyword )
         engine.groupsManager.groupsForMode( BPP.GroupingMode.PRE ).forEach( g => g.fileItems.forEach( fi =>
         {
            let s = session[ b2Norm( fi.filePath ) ];
            if ( s )
               fi.fileKeywords[ job.sessionKeyword ] = s;
         } ) );
      engine.rebuild();

      out.groups = engine.groupsManager.groups.map( g => ( {
         mode: g.mode == BPP.GroupingMode.POST ? "post" : "pre", type: String( g.imageType ), filter: g.filter,
         n: g.fileItems.length, keywords: g.keywords,
         fastIntegration: g.fastIntegrationData ? !!g.fastIntegrationData.enabled : null } ) );
      save();
      if ( job.dryRun )
      {
         out.phase = "done";
         out.ok = true;
         return;
      }

      if ( job.eventScript )
      {
         engine.usePipelineScript = true;
         engine.pipelineScriptFile = job.eventScript;
      }
      out.phase = "run";
      save();
      CoreApplication.beginNonInteractive();
      engine.nonInteractiveExecution = true;
      try
      {
         engine.cleanProcessLog();
         engine.pipelineManager.initializeExecution();
         engine.pipelineManager.buildExecutionPipeline();
         engine.pipelineManager.runPipeline();
         engine.pipelineManager.postExecutionActions();
         out.interrupted = !!engine.operationQueue.interruptionRequested;
      }
      finally
      {
         engine.nonInteractiveExecution = false;
         CoreApplication.endNonInteractive();
         try { console.flush(); engine.consoleLogger.stopLogging(); } catch ( x ) {}
      }
      out.logFile = engine.logsFileName;
      out.phase = "done";
      out.ok = !out.interrupted;
   }
   catch ( x )
   {
      out.error = String( x.message ?? x );
      out.stack = String( x.stack ?? "" );
   }
   finally
   {
      out.finished = ( new Date ).toISOString();
      save();
   }
}

b2Run();
