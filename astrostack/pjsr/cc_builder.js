// b2stacker – WBPP "Pipeline Builder Script": CosmeticCorrection (Auto Detect) ohne Darks und ohne Prozess-Icon.
//
// WBPP wendet CosmeticCorrection nur über ein Prozess-Icon an; im Automationsmodus (-n) gibt es keine Icons.
// Dieses Script wird von WBPP selbst ausgeführt (Parameter usePipelineBuilderScript / pipelineBuilderScriptFile,
// eval im Kontext der WBPP-Engine: "this" = engine). Es
//   1. liefert für den Icon-Namen "b2stacker_CC" eine fertig eingestellte CosmeticCorrection-Instanz,
//   2. setzt diesen Namen bei allen Light-Gruppen (= WBPP-eigener Schritt „Cosmetic Correction“ nach dem
//      Kalibrieren, mit Cache, Protokoll und Dateizuordnung wie gewohnt),
//   3. baut danach die normale Light-Pipeline von WBPP.
// Schlägt etwas fehl, baut WBPP die Standard-Pipeline (ohne Korrektur) – b2stacker erkennt das im Protokoll.
{
   let B2_ICON = "b2stacker_CC";
   let B2_HOT = __HOT__;
   let B2_COLD = __COLD__;
   if ( !ProcessInstance.__b2_fromIcon )
   {
      ProcessInstance.__b2_fromIcon = ProcessInstance.fromIcon;
      ProcessInstance.fromIcon = function( id )
      {
         if ( id == B2_ICON )
         {
            let CC = new CosmeticCorrection;
            CC.useMasterDark = false;
            CC.useDefectList = false;
            CC.useAutoDetect = true;
            CC.hotAutoCheck = true;
            CC.hotAutoValue = B2_HOT;
            CC.coldAutoCheck = B2_COLD > 0;
            CC.coldAutoValue = B2_COLD > 0 ? B2_COLD : 3.0;
            CC.amount = 1.0;
            return CC;
         }
         return ProcessInstance.__b2_fromIcon( id );
      };
   }
   let b2Groups = this.groupsManager.groupsForMode( WBPPGroupingMode.PRE );
   let b2Count = 0;
   for ( let i = 0; i < b2Groups.length; ++i )
      if ( b2Groups[ i ].imageType == ImageType.LIGHT )
      {
         b2Groups[ i ].setCcTemplate( B2_ICON );
         b2Count++;
      }
   console.noteln( "b2stacker: CosmeticCorrection (Auto Detect, hot " + B2_HOT + " sigma) fuer " + b2Count + " Light-Gruppe(n) aktiviert" );
   this.buildPipelineForLight();
}
